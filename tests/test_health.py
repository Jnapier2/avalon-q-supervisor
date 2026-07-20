import unittest

from avalon_q_supervisor.config import HealthSettings
from avalon_q_supervisor.health import HealthClassifier, HealthState
from avalon_q_supervisor.models import DeviceSnapshot


def snapshot(**overrides):
    values = {
        "observed_at": 1000.0,
        "reachable": True,
        "hashrate_ths": 85.0,
        "temperature_c": 65.0,
        "accepted_shares": 100,
        "rejected_shares": 1,
        "elapsed_seconds": 600,
        "pool_active": True,
        "pool_matches_expected": True,
        "model": "Q",
        "identity_verified": True,
    }
    values.update(overrides)
    return DeviceSnapshot(**values)


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.classifier = HealthClassifier(HealthSettings(expected_hashrate_ths=90.0))

    def test_healthy_snapshot(self):
        result = self.classifier.assess(snapshot())
        self.assertEqual(HealthState.HEALTHY, result.state)

    def test_offline_is_not_rebootable(self):
        result = self.classifier.assess(snapshot(reachable=False, error_code="timeout"))
        self.assertEqual(HealthState.OFFLINE, result.state)
        self.assertFalse(result.recoverable)

    def test_missing_telemetry_is_unknown(self):
        result = self.classifier.assess(
            DeviceSnapshot(observed_at=1000.0, reachable=True)
        )
        self.assertEqual(HealthState.UNKNOWN, result.state)

    def test_each_missing_core_signal_prevents_healthy_state(self):
        for field in ("hashrate_ths", "temperature_c", "pool_active", "elapsed_seconds"):
            with self.subTest(field=field):
                result = self.classifier.assess(snapshot(**{field: None}))
                self.assertEqual(HealthState.UNKNOWN, result.state)
                self.assertFalse(result.recoverable)

    def test_missing_temperature_blocks_zero_hash_recovery(self):
        result = self.classifier.assess(snapshot(hashrate_ths=0.0, temperature_c=None))
        self.assertEqual(HealthState.CRITICAL, result.state)
        self.assertFalse(result.recoverable)
        self.assertIn("telemetry-incomplete", result.reasons)

    def test_missing_hashrate_blocks_pool_recovery(self):
        result = self.classifier.assess(snapshot(hashrate_ths=None, pool_active=False))
        self.assertEqual(HealthState.CRITICAL, result.state)
        self.assertFalse(result.recoverable)

    def test_missing_pool_blocks_hash_recovery(self):
        result = self.classifier.assess(snapshot(hashrate_ths=0.0, pool_active=None))
        self.assertEqual(HealthState.CRITICAL, result.state)
        self.assertFalse(result.recoverable)

    def test_identity_mismatch_blocks_recovery(self):
        result = self.classifier.assess(snapshot(hashrate_ths=0.0, identity_verified=False))
        self.assertEqual(HealthState.CRITICAL, result.state)
        self.assertFalse(result.recoverable)
        self.assertIn("device-identity-mismatch", result.reasons)

    def test_unverified_identity_prevents_healthy_state(self):
        result = self.classifier.assess(snapshot(identity_verified=None, model=None))
        self.assertEqual(HealthState.UNKNOWN, result.state)
        self.assertFalse(result.recoverable)

    def test_nonfinite_snapshot_is_rejected_at_boundary(self):
        with self.assertRaisesRegex(ValueError, "finite"):
            snapshot(hashrate_ths=float("nan"))

    def test_low_hashrate_is_degraded(self):
        result = self.classifier.assess(snapshot(hashrate_ths=50.0))
        self.assertEqual(HealthState.DEGRADED, result.state)
        self.assertIn("hashrate-below-target", result.reasons)

    def test_hashrate_stall_is_recoverable_critical(self):
        result = self.classifier.assess(snapshot(hashrate_ths=0.0))
        self.assertEqual(HealthState.CRITICAL, result.state)
        self.assertTrue(result.recoverable)

    def test_thermal_critical_requires_operator(self):
        result = self.classifier.assess(snapshot(temperature_c=90.0, hashrate_ths=0.0))
        self.assertEqual(HealthState.CRITICAL, result.state)
        self.assertFalse(result.recoverable)
        self.assertIn("temperature-critical", result.reasons)

    def test_startup_grace_prevents_early_hash_recovery(self):
        result = self.classifier.assess(snapshot(hashrate_ths=0.0, elapsed_seconds=100))
        self.assertEqual(HealthState.DEGRADED, result.state)
        self.assertFalse(result.recoverable)
        self.assertIn("startup-grace", result.reasons)

    def test_missing_elapsed_cannot_bypass_startup_grace(self):
        result = self.classifier.assess(snapshot(hashrate_ths=0.0, elapsed_seconds=None))
        self.assertEqual(HealthState.UNKNOWN, result.state)
        self.assertFalse(result.recoverable)
        self.assertIn("missing-elapsed", result.reasons)

    def test_pool_mismatch_blocks_otherwise_recoverable_critical_state(self):
        result = self.classifier.assess(snapshot(hashrate_ths=0.0, pool_matches_expected=False))
        self.assertEqual(HealthState.CRITICAL, result.state)
        self.assertFalse(result.recoverable)

    def test_pool_mismatch_is_degraded(self):
        result = self.classifier.assess(snapshot(pool_matches_expected=False))
        self.assertEqual(HealthState.DEGRADED, result.state)
        self.assertIn("pool-endpoint-mismatch", result.reasons)

    def test_reject_ratio_uses_minimum_sample(self):
        large = self.classifier.assess(snapshot(accepted_shares=90, rejected_shares=10))
        small = self.classifier.assess(snapshot(accepted_shares=8, rejected_shares=2))
        self.assertEqual(HealthState.DEGRADED, large.state)
        self.assertEqual(HealthState.HEALTHY, small.state)


if __name__ == "__main__":
    unittest.main()
