import json
import os
import tempfile
import unittest
from pathlib import Path

from avalon_q_supervisor.config import apply_cli_overrides, load_config, validate_live_host


class ConfigTests(unittest.TestCase):
    def test_example_is_valid_and_non_executing(self):
        config = load_config("config.example.json", environ={})
        self.assertEqual("192.0.2.10", config.device.host)
        self.assertEqual(4028, config.device.port)
        self.assertFalse(config.recovery.enabled)
        self.assertEqual("Q", config.device.expected_model)
        self.assertEqual("stratum+tcp://stratum.ckpool.org:3333", config.health.expected_pool_url)

    def test_environment_overrides_json(self):
        config = load_config(
            "config.example.json",
            environ={
                "AVALON_Q_HOST": "device.local",
                "AVALON_Q_PORT": "4400",
                "AVALON_Q_EXPECTED_HASHRATE_THS": "88.5",
                "AVALON_Q_RECOVERY_ENABLED": "true",
            },
        )
        self.assertEqual("device.local", config.device.host)
        self.assertEqual(4400, config.device.port)
        self.assertEqual(88.5, config.health.expected_hashrate_ths)
        self.assertTrue(config.recovery.enabled)

    def test_cli_override_has_highest_precedence(self):
        config = load_config("config.example.json", environ={"AVALON_Q_HOST": "env.local"})
        config = apply_cli_overrides(config, host="cli.local", port=4028)
        self.assertEqual("cli.local", config.device.host)

    def test_unknown_settings_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "bad.json")
            path.write_text(json.dumps({"device": {"host": "x", "credential": "secret"}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown device setting"):
                load_config(path, environ={})

    def test_invalid_threshold_order_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "bad.json")
            path.write_text(
                json.dumps({"health": {"warning_temperature_c": 90, "critical_temperature_c": 80}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "warning temperature"):
                load_config(path, environ={})

    def test_live_host_accepts_explicit_local_addresses(self):
        for host in ("10.0.0.50", "172.16.0.50", "192.168.1.50", "127.0.0.1", "169.254.1.50", "::1", "fd00::50"):
            with self.subTest(host=host):
                validate_live_host(host)

    def test_live_host_rejects_hostname_public_and_documentation_addresses(self):
        for host in ("device.local", "8.8.8.8", "192.0.2.10", "2001:4860:4860::8888"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_live_host(host)

    def test_quoted_recovery_boolean_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "bad.json")
            path.write_text('{"recovery":{"enabled":"false"}}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "recovery.enabled must be bool"):
                load_config(path, environ={})

    def test_nonfinite_json_constants_are_rejected(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant), tempfile.TemporaryDirectory() as directory:
                path = Path(directory, "bad.json")
                path.write_text(
                    '{"health":{"critical_temperature_c":' + constant + '}}', encoding="utf-8"
                )
                with self.assertRaisesRegex(ValueError, "non-finite JSON constant"):
                    load_config(path, environ={})

    def test_integer_fields_reject_float_and_boolean(self):
        for value in ("4028.0", "true"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory, "bad.json")
                path.write_text('{"device":{"port":' + value + '}}', encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "port must be an integer"):
                    load_config(path, environ={})

    def test_duplicate_json_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "bad.json")
            path.write_text('{"recovery":{"enabled":false,"enabled":true}}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                load_config(path, environ={})

    def test_audit_state_and_lock_paths_must_be_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            shared = str(Path(directory, "shared.json")).replace("\\", "\\\\")
            for audit_suffix in ("", ".lock"):
                with self.subTest(audit_suffix=audit_suffix):
                    path = Path(directory, f"bad-{len(audit_suffix)}.json")
                    path.write_text(
                        '{"recovery":{"state_file":"'
                        + shared
                        + '"},"audit":{"log_file":"'
                        + shared
                        + audit_suffix
                        + '"}}',
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, "must be distinct"):
                        load_config(path, environ={})

    def test_existing_hardlink_path_aliases_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory, "state.json")
            audit = Path(directory, "audit.jsonl")
            state.write_text("placeholder", encoding="utf-8")
            try:
                os.link(state, audit)
            except OSError as exc:
                self.skipTest(f"hardlinks are unavailable: {exc}")
            config_path = Path(directory, "config.json")
            config_path.write_text(
                json.dumps(
                    {
                        "recovery": {"state_file": str(state)},
                        "audit": {"log_file": str(audit)},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "must be distinct"):
                load_config(config_path, environ={})


if __name__ == "__main__":
    unittest.main()
