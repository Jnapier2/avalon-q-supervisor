import socket
import unittest

from avalon_q_supervisor.adapter import (
    AdapterError,
    AvalonQAdapter,
    CanaanTcpClient,
    TcpSettings,
    parse_legacy_response,
)
from avalon_q_supervisor.config import HealthSettings
from avalon_q_supervisor.health import HealthClassifier, HealthState


SUMMARY = (
    "STATUS=S,When=100,Code=11,Msg=Summary,Description=cgminer 4.11.1|"
    "SUMMARY,Elapsed=1200,MHS av=84000000.00,MHS 5s=82000000.00,Accepted=100,Rejected=2|"
)
ESTATS = (
    "STATUS=S,When=100,Code=70,Msg=CGMiner stats|"
    "STATS=0,ID=AVALON0,MM ID0:Summary='STATS':{TMax[68] Fan1[1301] Fan2[1302] "
    "Fan3[1303] Fan4[1304] GHSavg[83000.00]}|"
)
POOLS = (
    "STATUS=S,When=100,Code=7,Msg=2 Pool(s)|"
    "POOL=0,URL=stratum+tcp://stratum.ckpool.org:3333,Status=Alive,Priority=0,"
    "Accepted=100,Rejected=2,Stratum Active=true|"
    "POOL=1,URL=stratum+tcp://example.invalid:3333,Status=Alive,Priority=1,"
    "Accepted=0,Rejected=0,Stratum Active=false|"
)
VERSION = "STATUS=S,When=100,Code=22,Msg=CGMiner versions|VERSION,PROD=Avalon Q,MODEL=Q,DNA=secret|"


class FakeConnection:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = b""
        self.timeout = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, payload):
        self.sent += payload

    def recv(self, _size):
        if not self.chunks:
            return b""
        value = self.chunks.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class StepClock:
    def __init__(self, step):
        self.value = -step
        self.step = step

    def __call__(self):
        self.value += self.step
        return self.value


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.commands = []

    def request(self, command):
        self.commands.append(command)
        response = self.responses[command]
        if isinstance(response, Exception):
            raise response
        return response


class AdapterTests(unittest.TestCase):
    def test_parser_preserves_repeated_pool_sections(self):
        sections = parse_legacy_response(POOLS)
        pools = [section for section in sections if "POOL" in section]
        self.assertEqual(["0", "1"], [pool["POOL"] for pool in pools])
        self.assertEqual("stratum+tcp://stratum.ckpool.org:3333", pools[0]["URL"])

    def test_tcp_client_sends_documented_plain_text_command(self):
        response = b"STATUS=S,Msg=ok|SUMMARY,Elapsed=10|"
        connection = FakeConnection([response])

        def factory(address, timeout):
            self.assertEqual(("192.168.1.50", 4028), address)
            self.assertEqual(2.0, timeout)
            return connection

        client = CanaanTcpClient(TcpSettings("192.168.1.50", timeout_seconds=2.0), factory)
        self.assertEqual(response.decode(), client.request("summary"))
        self.assertEqual(b"summary", connection.sent)

    def test_tcp_client_reads_sections_split_across_packets(self):
        connection = FakeConnection([b"STATUS=S,Msg=ok|", b"SUMMARY,Elapsed=10|"])
        client = CanaanTcpClient(TcpSettings("192.168.1.50"), lambda *_args, **_kwargs: connection)
        self.assertEqual("STATUS=S,Msg=ok|SUMMARY,Elapsed=10|", client.request("summary"))

    def test_tcp_client_rejects_control_characters(self):
        client = CanaanTcpClient(TcpSettings("192.168.1.50"), lambda *_args, **_kwargs: None)
        with self.assertRaises(ValueError):
            client.request("summary\nreboot")

    def test_transport_rejects_nonlocal_target_before_factory(self):
        called = False

        def factory(*_args, **_kwargs):
            nonlocal called
            called = True

        with self.assertRaises(ValueError):
            CanaanTcpClient(TcpSettings("8.8.8.8"), factory)
        self.assertFalse(called)

    def test_truncated_frame_is_rejected_even_on_eof(self):
        connection = FakeConnection([b"STATUS=S,Msg=Summary|"])
        client = CanaanTcpClient(TcpSettings("192.168.1.50"), lambda *_args, **_kwargs: connection)
        with self.assertRaisesRegex(AdapterError, "complete protocol frame") as raised:
            client.request("summary")
        self.assertEqual("incomplete-response", raised.exception.code)

    def test_duplicate_response_field_is_contained_as_invalid_telemetry(self):
        malformed = "STATUS=S,Msg=Summary|SUMMARY,Elapsed=10,Elapsed=20|"
        client = FakeClient({"summary": malformed})
        snapshot = AvalonQAdapter(client).poll(observed_at=123.0)
        self.assertFalse(snapshot.reachable)
        self.assertEqual("invalid-response", snapshot.error_code)

    def test_trickle_cannot_extend_total_deadline(self):
        connection = FakeConnection([b"S", b"T", b"A", b"T", b"U", b"S"])
        client = CanaanTcpClient(
            TcpSettings("192.168.1.50", timeout_seconds=1.0),
            lambda *_args, **_kwargs: connection,
            monotonic=StepClock(0.25),
        )
        with self.assertRaises(AdapterError) as raised:
            client.request("summary")
        self.assertEqual("incomplete-response", raised.exception.code)
        self.assertGreater(len(connection.chunks), 0)

    def test_timeout_after_complete_frame_is_accepted(self):
        response = b"STATUS=S,Msg=Summary|SUMMARY,Elapsed=10|"
        connection = FakeConnection([response, socket.timeout()])
        client = CanaanTcpClient(TcpSettings("192.168.1.50"), lambda *_args, **_kwargs: connection)
        self.assertEqual(response.decode(), client.request("summary"))

    def test_poll_normalizes_official_fields(self):
        client = FakeClient({"summary": SUMMARY, "estats": ESTATS, "pools": POOLS, "version": VERSION})
        snapshot = AvalonQAdapter(client).poll(
            expected_pool_url="stratum+tcp://stratum.ckpool.org:3333", observed_at=123.0
        )
        self.assertTrue(snapshot.reachable)
        self.assertAlmostEqual(82.0, snapshot.hashrate_ths)
        self.assertEqual(68.0, snapshot.temperature_c)
        self.assertEqual((1301, 1302, 1303, 1304), snapshot.fan_rpm)
        self.assertEqual(100, snapshot.accepted_shares)
        self.assertEqual(2, snapshot.rejected_shares)
        self.assertTrue(snapshot.pool_active)
        self.assertTrue(snapshot.pool_matches_expected)
        self.assertEqual("Q", snapshot.model)
        self.assertTrue(snapshot.identity_verified)

    def test_identity_mismatch_is_explicit(self):
        client = FakeClient({"summary": SUMMARY, "estats": ESTATS, "pools": POOLS, "version": VERSION})
        snapshot = AvalonQAdapter(client).poll(expected_model="Different Model", observed_at=123.0)
        self.assertFalse(snapshot.identity_verified)

    def test_identity_recheck_queries_version(self):
        client = FakeClient({"version": VERSION})
        adapter = AvalonQAdapter(client)
        self.assertTrue(adapter.verify_identity("Q"))
        self.assertFalse(adapter.verify_identity("Different Model"))
        self.assertEqual(["version", "version"], client.commands)

    def test_summary_failure_returns_offline_without_raw_error(self):
        client = FakeClient({"summary": AdapterError("timeout", "contains private host")})
        snapshot = AvalonQAdapter(client).poll(observed_at=123.0)
        self.assertFalse(snapshot.reachable)
        self.assertEqual("timeout", snapshot.error_code)
        self.assertNotIn("private", str(snapshot))

    def test_zero_short_window_hashrate_is_not_masked_by_average(self):
        summary = SUMMARY.replace("MHS 5s=82000000.00", "MHS 5s=0.00")
        client = FakeClient({"summary": summary, "estats": ESTATS, "pools": POOLS, "version": VERSION})
        snapshot = AvalonQAdapter(client).poll(observed_at=123.0)
        self.assertEqual(0.0, snapshot.hashrate_ths)

    def test_nonfinite_hashrate_becomes_incomplete_and_nonrecoverable(self):
        summary = SUMMARY.replace("MHS av=84000000.00", "MHS av=NaN").replace(
            "MHS 5s=82000000.00", "MHS 5s=Infinity"
        )
        estats = ESTATS.replace(" GHSavg[83000.00]", "")
        client = FakeClient({"summary": summary, "estats": estats, "pools": POOLS, "version": VERSION})
        snapshot = AvalonQAdapter(client).poll(observed_at=123.0)
        result = HealthClassifier(HealthSettings(expected_hashrate_ths=90.0)).assess(snapshot)
        self.assertIsNone(snapshot.hashrate_ths)
        self.assertEqual(HealthState.UNKNOWN, result.state)
        self.assertFalse(result.recoverable)

    def test_missing_or_invalid_pool_activity_remains_unknown(self):
        variants = (
            "STATUS=S,When=100,Code=7,Msg=1 Pool(s)|"
            "POOL=0,URL=stratum+tcp://stratum.ckpool.org:3333,Priority=0|",
            "STATUS=S,When=100,Code=7,Msg=1 Pool(s)|"
            "POOL=0,URL=stratum+tcp://stratum.ckpool.org:3333,Status=Alive,Priority=0,"
            "Stratum Active=maybe|",
        )
        for pools in variants:
            with self.subTest(pools=pools):
                summary = SUMMARY.replace("MHS 5s=82000000.00", "MHS 5s=0.00")
                client = FakeClient(
                    {"summary": summary, "estats": ESTATS, "pools": pools, "version": VERSION}
                )
                snapshot = AvalonQAdapter(client).poll(observed_at=123.0)
                result = HealthClassifier(HealthSettings(expected_hashrate_ths=90.0)).assess(snapshot)
                self.assertIsNone(snapshot.pool_active)
                self.assertEqual(HealthState.CRITICAL, result.state)
                self.assertFalse(result.recoverable)
                self.assertIn("missing-pool", result.reasons)

    def test_oversized_device_fields_do_not_escape_normalization(self):
        very_long_number = "9" * 5000
        very_long_model = "Q" * 5000
        variants = (
            {
                "estats": ESTATS.replace("Fan1[1301]", f"Fan1[{very_long_number}]"),
                "pools": POOLS,
                "version": VERSION,
            },
            {
                "estats": ESTATS,
                "pools": (
                    "STATUS=S,When=100,Code=7,Msg=1 Pool(s)|"
                    "POOL=0,URL=stratum+tcp://stratum.ckpool.org:3333,Status=Alive,"
                    f"Priority={very_long_number}|"
                ),
                "version": VERSION,
            },
            {
                "estats": ESTATS,
                "pools": POOLS,
                "version": (
                    "STATUS=S,When=100,Code=22,Msg=CGMiner versions|"
                    f"VERSION,PROD={very_long_model},MODEL={very_long_model}|"
                ),
            },
        )
        for variant in variants:
            with self.subTest(fields=tuple(variant)):
                client = FakeClient({"summary": SUMMARY, **variant})
                snapshot = AvalonQAdapter(client).poll(observed_at=123.0)
                self.assertTrue(snapshot.reachable)

    def test_reboot_uses_exact_documented_command(self):
        client = FakeClient({AvalonQAdapter.REBOOT_COMMAND: "STATUS=S,Code=119,Msg=ASC 0 set OK|"})
        response = AvalonQAdapter(client).reboot()
        self.assertEqual([AvalonQAdapter.REBOOT_COMMAND], client.commands)
        self.assertEqual("S", response[0]["STATUS"])


if __name__ == "__main__":
    unittest.main()
