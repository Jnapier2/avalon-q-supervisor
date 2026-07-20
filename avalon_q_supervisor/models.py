"""Shared data structures for observations and decisions."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class DeviceSnapshot:
    """A privacy-conscious, normalized device observation."""

    observed_at: float
    reachable: bool
    hashrate_ths: float | None = None
    temperature_c: float | None = None
    fan_rpm: tuple[int, ...] = ()
    accepted_shares: int | None = None
    rejected_shares: int | None = None
    elapsed_seconds: int | None = None
    pool_active: bool | None = None
    pool_matches_expected: bool | None = None
    model: str | None = None
    identity_verified: bool | None = None
    error_code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_finite_number("observed_at", self.observed_at, minimum=0)
        if type(self.reachable) is not bool:
            raise ValueError("reachable must be a boolean")
        _require_optional_finite_number("hashrate_ths", self.hashrate_ths, minimum=0)
        _require_optional_finite_number("temperature_c", self.temperature_c, minimum=-100, maximum=200)
        if type(self.fan_rpm) is not tuple or any(type(value) is not int or value < 0 for value in self.fan_rpm):
            raise ValueError("fan_rpm must be a tuple of nonnegative integers")
        for name, value in (
            ("accepted_shares", self.accepted_shares),
            ("rejected_shares", self.rejected_shares),
            ("elapsed_seconds", self.elapsed_seconds),
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a nonnegative integer or null")
        for name, value in (
            ("pool_active", self.pool_active),
            ("pool_matches_expected", self.pool_matches_expected),
            ("identity_verified", self.identity_verified),
        ):
            if value is not None and type(value) is not bool:
                raise ValueError(f"{name} must be a boolean or null")
        if self.model is not None and (type(self.model) is not str or not self.model.strip() or len(self.model) > 100):
            raise ValueError("model must be a short non-empty string or null")
        if self.error_code is not None and (
            type(self.error_code) is not str or not self.error_code.strip() or len(self.error_code) > 100
        ):
            raise ValueError("error_code must be a short non-empty string or null")
        if type(self.metadata) is not dict:
            raise ValueError("metadata must be a dictionary")

    @property
    def reject_ratio(self) -> float | None:
        if self.accepted_shares is None or self.rejected_shares is None:
            return None
        total = self.accepted_shares + self.rejected_shares
        return self.rejected_shares / total if total > 0 else None

    def audit_metrics(self) -> dict[str, Any]:
        """Return the small, non-identifying metric set allowed in audit logs."""
        return {
            "reachable": self.reachable,
            "hashrate_ths": self.hashrate_ths,
            "temperature_c": self.temperature_c,
            "fan_rpm": list(self.fan_rpm),
            "accepted_shares": self.accepted_shares,
            "rejected_shares": self.rejected_shares,
            "reject_ratio": self.reject_ratio,
            "elapsed_seconds": self.elapsed_seconds,
            "pool_active": self.pool_active,
            "pool_matches_expected": self.pool_matches_expected,
            "model": self.model,
            "identity_verified": self.identity_verified,
            "error_code": self.error_code,
        }


def _require_finite_number(
    name: str,
    value: object,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> None:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not finite:
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} cannot be below {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} cannot exceed {maximum}")


def _require_optional_finite_number(
    name: str,
    value: object,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> None:
    if value is not None:
        _require_finite_number(name, value, minimum=minimum, maximum=maximum)


_SNAPSHOT_FIELDS = set(DeviceSnapshot.__dataclass_fields__)


def snapshot_from_mapping(value: Mapping[str, Any]) -> DeviceSnapshot:
    """Validate an untrusted fixture before constructing a snapshot."""
    if type(value) is not dict:
        raise ValueError("each demo observation must be a JSON object")
    unknown = set(value) - _SNAPSHOT_FIELDS
    if unknown:
        raise ValueError(f"unknown snapshot field(s): {', '.join(sorted(unknown))}")
    missing = {"observed_at", "reachable"} - set(value)
    if missing:
        raise ValueError(f"missing snapshot field(s): {', '.join(sorted(missing))}")
    data = dict(value)
    fan_rpm = data.get("fan_rpm", [])
    if type(fan_rpm) is not list or any(type(item) is not int for item in fan_rpm):
        raise ValueError("fixture fan_rpm must be an array of integers")
    data["fan_rpm"] = tuple(fan_rpm)
    if "metadata" in data and type(data["metadata"]) is not dict:
        raise ValueError("fixture metadata must be a JSON object")
    return DeviceSnapshot(**data)
