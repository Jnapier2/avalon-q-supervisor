import json
import tempfile
import unittest
from pathlib import Path

from avalon_q_supervisor.audit import JsonlAuditLogger
from avalon_q_supervisor.config import HealthSettings, RecoverySettings
from avalon_q_supervisor.health import HealthClassifier
from avalon_q_supervisor.models import DeviceSnapshot
from avalon_q_supervisor.policy import RecoveryAction, RecoveryPolicy
from avalon_q_supervisor.supervisor import AvalonQSupervisor


class FakeAdapter:
    def __init__(self, audit_path=None, identity_result=True):
        self.reboot_calls = 0
        self.identity_checks = 0
        self.identity_result = identity_result
        self.audit_path = Path(audit_path) if audit_path else None
        self.intent_seen_before_reboot = False

    def verify_identity(self, _expected_model):
        self.identity_checks += 1
        return self.identity_result

    def reboot(self):
        if self.audit_path:
            events = [json.loads(line) for line in self.audit_path.read_text().splitlines()]
            self.intent_seen_before_reboot = bool(events and events[-1]["event"] == "recovery-intent")
        self.reboot_calls += 1
        return [{"STATUS": "S"}]


class FailingAudit:
    def write(self, *_args, **_kwargs):
        raise OSError("simulated audit failure")


def critical_snapshot(observed_at):
    return DeviceSnapshot(
        observed_at=observed_at,
        reachable=True,
        hashrate_ths=0.0,
        temperature_c=60.0,
        accepted_shares=100,
        rejected_shares=1,
        elapsed_seconds=1000,
        pool_active=True,
        model="Q",
        identity_verified=True,
    )


class SupervisorTests(unittest.TestCase):
    def build(self, directory, *, execute, consecutive=2, audit=None, identity_result=True):
        audit_path = Path(directory, "audit.jsonl")
        adapter = FakeAdapter(audit_path, identity_result=identity_result)
        settings = RecoverySettings(
            enabled=True,
            consecutive_critical=consecutive,
            cooldown_seconds=10,
            window_seconds=100,
            max_reboots_per_window=1,
            state_file="unused.json",
        )
        supervisor = AvalonQSupervisor(
            adapter=adapter,
            classifier=HealthClassifier(HealthSettings(expected_hashrate_ths=90.0)),
            policy=RecoveryPolicy(settings, state_path=Path(directory, "state.json")),
            audit=audit or JsonlAuditLogger(audit_path),
            device_ref="device-local",
            execute_requested=execute,
        )
        return supervisor, adapter

    def test_dry_run_never_calls_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor, adapter = self.build(directory, execute=False)
            supervisor.run_once(snapshot=critical_snapshot(10), now=10)
            result = supervisor.run_once(snapshot=critical_snapshot(20), now=20)
            self.assertEqual(RecoveryAction.WOULD_REBOOT, result.decision.action)
            self.assertEqual(0, adapter.reboot_calls)
            self.assertEqual("dry-run", result.command_outcome)

    def test_execute_reserves_and_sends_once(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor, adapter = self.build(directory, execute=True)
            supervisor.run_once(snapshot=critical_snapshot(10), now=10)
            result = supervisor.run_once(snapshot=critical_snapshot(20), now=20)
            self.assertEqual(RecoveryAction.REBOOT, result.decision.action)
            self.assertEqual(1, adapter.reboot_calls)
            self.assertEqual(1, adapter.identity_checks)
            self.assertEqual("accepted", result.command_outcome)
            self.assertTrue(adapter.intent_seen_before_reboot)

            events = [json.loads(line) for line in Path(directory, "audit.jsonl").read_text().splitlines()]
            self.assertEqual(3, len(events))
            self.assertEqual("recovery-intent", events[-2]["event"])
            self.assertEqual("recovery-outcome", events[-1]["event"])
            self.assertEqual("critical", events[-1]["health"]["state"])
            self.assertEqual("reboot", events[-1]["recovery"]["action"])
            self.assertNotIn("192.", json.dumps(events))

    def test_audit_failure_blocks_reservation_and_command(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor, adapter = self.build(
                directory,
                execute=True,
                consecutive=1,
                audit=FailingAudit(),
            )
            result = supervisor.run_once(snapshot=critical_snapshot(10), now=10)
            self.assertEqual(RecoveryAction.SUPPRESSED_AUDIT, result.decision.action)
            self.assertEqual("blocked-audit-failure", result.command_outcome)
            self.assertEqual(0, adapter.reboot_calls)
            self.assertEqual([], supervisor.policy.read_state().attempt_times)

    def test_identity_mismatch_blocks_command(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor, adapter = self.build(directory, execute=True, consecutive=1)
            snapshot = critical_snapshot(10)
            mismatched = DeviceSnapshot(
                **{**snapshot.__dict__, "identity_verified": False, "model": "Different Model"}
            )
            result = supervisor.run_once(snapshot=mismatched, now=10)
            self.assertEqual(RecoveryAction.MANUAL_INTERVENTION, result.decision.action)
            self.assertEqual(0, adapter.reboot_calls)

    def test_identity_is_rechecked_before_command(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor, adapter = self.build(
                directory,
                execute=True,
                consecutive=1,
                identity_result=False,
            )
            result = supervisor.run_once(snapshot=critical_snapshot(10), now=10)
            self.assertEqual(RecoveryAction.REBOOT, result.decision.action)
            self.assertEqual("blocked-identity-recheck", result.command_outcome)
            self.assertEqual(1, adapter.identity_checks)
            self.assertEqual(0, adapter.reboot_calls)

    def test_corrupt_state_blocks_command(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory, "state.json")
            state_path.write_text("{truncated", encoding="utf-8")
            supervisor, adapter = self.build(directory, execute=True, consecutive=1)
            result = supervisor.run_once(snapshot=critical_snapshot(10), now=10)
            self.assertEqual(RecoveryAction.SUPPRESSED_STATE, result.decision.action)
            self.assertEqual("blocked-state-unavailable", result.command_outcome)
            self.assertEqual(0, adapter.reboot_calls)


if __name__ == "__main__":
    unittest.main()
