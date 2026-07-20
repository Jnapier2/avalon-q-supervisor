import multiprocessing
import tempfile
import unittest
from pathlib import Path

from avalon_q_supervisor.config import RecoverySettings
from avalon_q_supervisor.health import HealthAssessment, HealthState
from avalon_q_supervisor.policy import RecoveryAction, RecoveryPolicy, StateUnavailableError


CRITICAL = HealthAssessment(HealthState.CRITICAL, ("hashrate-stalled",), True)
THERMAL = HealthAssessment(HealthState.CRITICAL, ("temperature-critical",), False)
HEALTHY = HealthAssessment(HealthState.HEALTHY, ("within-configured-bounds",), False)


def _race_worker(state_path, intent_path, start_event, ready_queue, result_queue):
    settings = RecoverySettings(
        enabled=True,
        consecutive_critical=1,
        cooldown_seconds=60,
        window_seconds=3600,
        max_reboots_per_window=1,
        state_file=state_path,
    )
    policy = RecoveryPolicy(settings, state_path=state_path)
    ready_queue.put("ready")
    start_event.wait(10)

    def write_intent(_decision):
        Path(intent_path).write_text("intent\n", encoding="utf-8")

    decision = policy.decide_and_reserve(
        CRITICAL,
        now=1000.0,
        execute_requested=True,
        before_reserve=write_intent,
    )
    result_queue.put(decision.action.value)


class PolicyTests(unittest.TestCase):
    def settings(self, **overrides):
        values = {
            "enabled": True,
            "consecutive_critical": 3,
            "cooldown_seconds": 10,
            "window_seconds": 100,
            "max_reboots_per_window": 2,
            "state_file": "unused.json",
        }
        values.update(overrides)
        return RecoverySettings(**values)

    @staticmethod
    def decide(policy, assessment, now, execute=False):
        return policy.decide_and_reserve(
            assessment,
            now=now,
            execute_requested=execute,
            before_reserve=(lambda _decision: None) if execute else None,
        )

    def test_requires_consecutive_evidence(self):
        policy = RecoveryPolicy(self.settings(), state_path=None)
        self.assertEqual(RecoveryAction.OBSERVE, self.decide(policy, CRITICAL, 1).action)
        self.assertEqual(RecoveryAction.OBSERVE, self.decide(policy, CRITICAL, 2).action)
        self.assertEqual(RecoveryAction.WOULD_REBOOT, self.decide(policy, CRITICAL, 3).action)

    def test_healthy_state_resets_streak(self):
        policy = RecoveryPolicy(self.settings(), state_path=None)
        self.decide(policy, CRITICAL, 1)
        self.decide(policy, HEALTHY, 2)
        result = self.decide(policy, CRITICAL, 3)
        self.assertEqual(1, result.critical_streak)

    def test_disabled_configuration_suppresses_action(self):
        policy = RecoveryPolicy(self.settings(enabled=False, consecutive_critical=1), state_path=None)
        result = self.decide(policy, CRITICAL, 1, execute=True)
        self.assertEqual(RecoveryAction.SUPPRESSED_DISABLED, result.action)

    def test_thermal_condition_is_manual(self):
        policy = RecoveryPolicy(self.settings(consecutive_critical=1), state_path=None)
        result = self.decide(policy, THERMAL, 1, execute=True)
        self.assertEqual(RecoveryAction.MANUAL_INTERVENTION, result.action)

    def test_cooldown_applies_after_atomic_reservation(self):
        policy = RecoveryPolicy(self.settings(consecutive_critical=1), state_path=None)
        self.assertEqual(RecoveryAction.REBOOT, self.decide(policy, CRITICAL, 10, execute=True).action)
        result = self.decide(policy, CRITICAL, 15, execute=True)
        self.assertEqual(RecoveryAction.SUPPRESSED_COOLDOWN, result.action)

    def test_rolling_budget_caps_attempts(self):
        policy = RecoveryPolicy(
            self.settings(consecutive_critical=1, max_reboots_per_window=1), state_path=None
        )
        self.decide(policy, CRITICAL, 10, execute=True)
        result = self.decide(policy, CRITICAL, 25, execute=True)
        self.assertEqual(RecoveryAction.SUPPRESSED_BUDGET, result.action)

    def test_state_is_durable_and_reloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "state.json")
            first = RecoveryPolicy(self.settings(consecutive_critical=1), state_path=path)
            self.decide(first, CRITICAL, 10, execute=True)
            second = RecoveryPolicy(self.settings(consecutive_critical=1), state_path=path)
            result = self.decide(second, CRITICAL, 15, execute=True)
            self.assertEqual(RecoveryAction.SUPPRESSED_COOLDOWN, result.action)
            self.assertEqual([10.0], second.read_state().attempt_times)

    def test_missing_intent_writer_cannot_reserve(self):
        policy = RecoveryPolicy(self.settings(consecutive_critical=1), state_path=None)
        result = policy.decide_and_reserve(CRITICAL, now=10, execute_requested=True)
        self.assertEqual(RecoveryAction.SUPPRESSED_AUDIT, result.action)
        self.assertEqual([], policy.read_state().attempt_times)

    def test_corrupt_state_fails_closed_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "state.json")
            path.write_text("{truncated", encoding="utf-8")
            policy = RecoveryPolicy(self.settings(consecutive_critical=1), state_path=path)
            result = self.decide(policy, CRITICAL, 10, execute=True)
            self.assertEqual(RecoveryAction.SUPPRESSED_STATE, result.action)
            self.assertEqual("{truncated", path.read_text(encoding="utf-8"))
            with self.assertRaises(StateUnavailableError):
                policy.read_state()

    def test_nonfinite_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "state.json")
            path.write_text(
                '{"schema_version":1,"critical_streak":0,"condition_key":"",'
                '"attempt_times":[NaN],"last_attempt_at":null,"last_plan_at":null}\n',
                encoding="utf-8",
            )
            policy = RecoveryPolicy(self.settings(consecutive_critical=1), state_path=path)
            result = self.decide(policy, CRITICAL, 10, execute=True)
            self.assertEqual(RecoveryAction.SUPPRESSED_STATE, result.action)

    def test_unreadable_state_shape_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "state.json")
            path.mkdir()
            policy = RecoveryPolicy(self.settings(consecutive_critical=1), state_path=path)
            result = self.decide(policy, CRITICAL, 10, execute=True)
            self.assertEqual(RecoveryAction.SUPPRESSED_STATE, result.action)

    def test_dangling_state_symlink_is_not_treated_as_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "state.json")
            try:
                path.symlink_to(Path(directory, "missing-state.json"))
            except OSError as exc:
                self.skipTest(f"symbolic links are unavailable: {exc}")
            policy = RecoveryPolicy(self.settings(consecutive_critical=1), state_path=path)
            result = self.decide(policy, CRITICAL, 10, execute=True)
            self.assertEqual(RecoveryAction.SUPPRESSED_STATE, result.action)
            self.assertTrue(path.is_symlink())

    def test_two_processes_share_one_atomic_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            context = multiprocessing.get_context("spawn")
            state_path = str(Path(directory, "state.json"))
            start_event = context.Event()
            ready_queue = context.Queue()
            result_queue = context.Queue()
            processes = [
                context.Process(
                    target=_race_worker,
                    args=(
                        state_path,
                        str(Path(directory, f"intent-{index}.txt")),
                        start_event,
                        ready_queue,
                        result_queue,
                    ),
                )
                for index in range(2)
            ]
            for process in processes:
                process.start()
            self.assertEqual(["ready", "ready"], sorted(ready_queue.get(timeout=10) for _ in range(2)))
            start_event.set()
            actions = [result_queue.get(timeout=10) for _ in range(2)]
            for process in processes:
                process.join(timeout=10)
                self.assertFalse(process.is_alive())
                self.assertEqual(0, process.exitcode)

            self.assertEqual(1, actions.count(RecoveryAction.REBOOT.value))
            self.assertEqual(1, actions.count(RecoveryAction.SUPPRESSED_COOLDOWN.value))
            policy = RecoveryPolicy(self.settings(consecutive_critical=1), state_path=state_path)
            state = policy.read_state()
            self.assertEqual([1000.0], state.attempt_times)
            self.assertEqual(1000.0, state.last_attempt_at)


if __name__ == "__main__":
    unittest.main()
