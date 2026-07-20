"""Explicit health-state classification for normalized observations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .config import HealthSettings
from .models import DeviceSnapshot


class HealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class HealthAssessment:
    state: HealthState
    reasons: tuple[str, ...]
    recoverable: bool


class HealthClassifier:
    """Convert telemetry into deterministic, explainable operating states."""

    def __init__(self, settings: HealthSettings) -> None:
        self.settings = settings

    def assess(self, snapshot: DeviceSnapshot) -> HealthAssessment:
        if not snapshot.reachable:
            return HealthAssessment(HealthState.OFFLINE, (snapshot.error_code or "unreachable",), False)

        missing: list[str] = []
        if snapshot.hashrate_ths is None:
            missing.append("hashrate")
        if snapshot.temperature_c is None:
            missing.append("temperature")
        if snapshot.pool_active is None:
            missing.append("pool")
        if snapshot.elapsed_seconds is None:
            missing.append("elapsed")

        critical: list[str] = []
        degraded: list[str] = []
        unsafe = False
        beyond_grace = (
            snapshot.elapsed_seconds is not None
            and snapshot.elapsed_seconds >= self.settings.startup_grace_seconds
        )

        if snapshot.temperature_c is not None:
            if snapshot.temperature_c >= self.settings.critical_temperature_c:
                critical.append("temperature-critical")
                unsafe = True
            elif snapshot.temperature_c >= self.settings.warning_temperature_c:
                degraded.append("temperature-elevated")

        if snapshot.hashrate_ths is not None and beyond_grace:
            if snapshot.hashrate_ths <= 0:
                critical.append("hashrate-stalled")
            elif self.settings.expected_hashrate_ths > 0:
                ratio = snapshot.hashrate_ths / self.settings.expected_hashrate_ths
                if ratio <= self.settings.critical_hashrate_ratio:
                    critical.append("hashrate-stalled")
                elif ratio < self.settings.degraded_hashrate_ratio:
                    degraded.append("hashrate-below-target")

        if snapshot.pool_active is False and beyond_grace:
            critical.append("pool-unavailable")
        if snapshot.pool_matches_expected is False:
            degraded.append("pool-endpoint-mismatch")
        if not beyond_grace and (snapshot.hashrate_ths == 0 or snapshot.pool_active is False):
            degraded.append("startup-grace")

        total_shares = (snapshot.accepted_shares or 0) + (snapshot.rejected_shares or 0)
        if (
            snapshot.reject_ratio is not None
            and total_shares >= self.settings.minimum_shares_for_ratio
            and snapshot.reject_ratio > self.settings.max_reject_ratio
        ):
            degraded.append("reject-ratio-elevated")

        identity_problem: str | None = None
        if snapshot.identity_verified is False:
            identity_problem = "device-identity-mismatch"
        elif snapshot.identity_verified is None:
            identity_problem = "device-identity-unverified"

        if critical:
            reasons = critical + degraded
            if missing:
                reasons.extend(["telemetry-incomplete", *(f"missing-{field}" for field in missing)])
            if identity_problem:
                reasons.append(identity_problem)
            return HealthAssessment(
                HealthState.CRITICAL,
                tuple(reasons),
                not unsafe
                and not missing
                and identity_problem is None
                and snapshot.pool_matches_expected is not False,
            )
        if identity_problem:
            reasons = [identity_problem]
            if missing:
                reasons.extend(["telemetry-incomplete", *(f"missing-{field}" for field in missing)])
            reasons.extend(degraded)
            return HealthAssessment(HealthState.UNKNOWN, tuple(reasons), False)
        if missing:
            reasons = ["telemetry-incomplete", *(f"missing-{field}" for field in missing), *degraded]
            return HealthAssessment(HealthState.UNKNOWN, tuple(reasons), False)
        if degraded:
            return HealthAssessment(HealthState.DEGRADED, tuple(degraded), False)
        return HealthAssessment(HealthState.HEALTHY, ("within-configured-bounds",), False)
