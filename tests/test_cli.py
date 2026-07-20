import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from avalon_q_supervisor.__main__ import main


class CliTests(unittest.TestCase):
    def assert_cli_error(self, arguments):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = main(arguments)
        self.assertEqual(2, result)
        self.assertIn("error:", stderr.getvalue())

    def test_watch_rejects_negative_cycles(self):
        self.assert_cli_error(["--host", "127.0.0.1", "watch", "--cycles", "-1"])

    def test_watch_rejects_nonfinite_and_short_intervals(self):
        for value in ("nan", "inf", "-inf", "4.99", "86401"):
            with self.subTest(value=value):
                self.assert_cli_error(
                    ["--host", "127.0.0.1", "watch", f"--interval={value}", "--cycles", "1"]
                )

    def test_demo_rejects_nonfinite_fixture_value(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory, "fixture.json")
            fixture.write_text('[{"observed_at":NaN,"reachable":true}]', encoding="utf-8")
            self.assert_cli_error(
                ["--audit-log", str(Path(directory, "audit.jsonl")), "demo", "--fixture", str(fixture)]
            )


if __name__ == "__main__":
    unittest.main()
