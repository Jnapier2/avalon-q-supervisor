import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from avalon_q_supervisor.audit import JsonlAuditLogger, device_reference, sanitize


class AuditTests(unittest.TestCase):
    def test_sensitive_keys_are_redacted(self):
        document = sanitize(
            {
                "host": "198.51.100.42",
                "password": "do-not-log",
                "nested": {"worker": "bc1qexample"},
                "state": "healthy",
            }
        )
        self.assertEqual("[REDACTED]", document["host"])
        self.assertEqual("[REDACTED]", document["password"])
        self.assertEqual("[REDACTED]", document["nested"]["worker"])
        self.assertEqual("healthy", document["state"])

    def test_text_patterns_are_redacted(self):
        value = sanitize("device tcp://198.51.100.42:4028 has e0:e1:a9:3c:ec:aa")
        self.assertNotIn("198.51.100.42", value)
        self.assertNotIn("e0:e1", value)

    def test_bracketed_ipv6_endpoints_are_redacted(self):
        value = sanitize("tcp://[fd00::42]:4028 and [fe80::1%eth0]:4028")
        self.assertNotIn("fd00::42", value)
        self.assertNotIn("fe80::1", value)
        self.assertEqual(2, value.count("[REDACTED-IPV6]"))

    def test_embedded_unbracketed_ipv6_is_redacted(self):
        value = sanitize("peer=fd00::42 route=fe80::1%eth0")
        self.assertNotIn("fd00::42", value)
        self.assertNotIn("fe80::1", value)
        self.assertEqual(2, value.count("[REDACTED-IPV6]"))

    def test_ipv4_mapped_ipv6_is_fully_redacted(self):
        value = sanitize("peer=::ffff:192.168.1.1")
        self.assertEqual("peer=[REDACTED-IPV6]", value)

    def test_logger_writes_one_valid_event_per_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "audit.jsonl")
            logger = JsonlAuditLogger(path)
            logger.write({"event": "test", "host": "198.51.100.42"}, observed_at=0.0)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(1, len(lines))
            document = json.loads(lines[0])
            self.assertEqual("1970-01-01T00:00:00Z", document["timestamp"])
            self.assertEqual("[REDACTED]", document["host"])

    def test_logger_syncs_the_parent_directory_after_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "audit.jsonl")
            with patch("avalon_q_supervisor.audit._fsync_parent_directory") as sync_parent:
                JsonlAuditLogger(path).write({"event": "test"}, observed_at=0.0)
            sync_parent.assert_called_once_with(path)

    def test_unsalted_device_reference_is_not_derived_from_host(self):
        self.assertEqual("device-local", device_reference("198.51.100.42", ""))
        first = device_reference("198.51.100.42", "private-salt")
        second = device_reference("198.51.100.42", "private-salt")
        self.assertEqual(first, second)
        self.assertNotIn("192", first)

    def test_logger_rejects_nonfinite_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = JsonlAuditLogger(Path(directory, "audit.jsonl"))
            with self.assertRaises(ValueError):
                logger.write({"event": "test", "metric": float("nan")}, observed_at=0.0)


if __name__ == "__main__":
    unittest.main()
