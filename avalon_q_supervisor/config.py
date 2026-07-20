"""Strict configuration loading with environment and CLI override support."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .adapter import TcpSettings
from .network import validate_local_address


def _require_exact_type(name: str, value: object, expected: type) -> None:
    if type(value) is not expected:
        raise ValueError(f"{name} must be {expected.__name__}")


def _require_finite_number(name: str, value: object) -> None:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a number")
    try:
        finite = math.isfinite(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not finite:
        raise ValueError(f"{name} must be finite")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is not allowed: {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class HealthSettings:
    expected_hashrate_ths: float = 0.0
    degraded_hashrate_ratio: float = 0.70
    critical_hashrate_ratio: float = 0.10
    startup_grace_seconds: int = 300
    warning_temperature_c: float = 70.0
    critical_temperature_c: float = 85.0
    max_reject_ratio: float = 0.05
    minimum_shares_for_ratio: int = 20
    expected_pool_url: str = ""

    def __post_init__(self) -> None:
        for name in (
            "expected_hashrate_ths",
            "degraded_hashrate_ratio",
            "critical_hashrate_ratio",
            "warning_temperature_c",
            "critical_temperature_c",
            "max_reject_ratio",
        ):
            _require_finite_number(f"health.{name}", getattr(self, name))
        _require_exact_type("health.startup_grace_seconds", self.startup_grace_seconds, int)
        _require_exact_type("health.minimum_shares_for_ratio", self.minimum_shares_for_ratio, int)
        _require_exact_type("health.expected_pool_url", self.expected_pool_url, str)
        if self.expected_hashrate_ths < 0:
            raise ValueError("health.expected_hashrate_ths cannot be negative")
        if not 0 <= self.critical_hashrate_ratio < self.degraded_hashrate_ratio <= 1:
            raise ValueError("hashrate ratios must satisfy 0 <= critical < degraded <= 1")
        if self.warning_temperature_c >= self.critical_temperature_c:
            raise ValueError("warning temperature must be lower than critical temperature")
        if not 0 <= self.max_reject_ratio <= 1:
            raise ValueError("health.max_reject_ratio must be between 0 and 1")
        if self.startup_grace_seconds < 0 or self.minimum_shares_for_ratio < 0:
            raise ValueError("health grace and sample settings cannot be negative")
        if self.expected_pool_url:
            parsed_pool = urlsplit(self.expected_pool_url)
            if not parsed_pool.scheme or not parsed_pool.hostname:
                raise ValueError("health.expected_pool_url must be an absolute endpoint URL")
            if parsed_pool.username or parsed_pool.password:
                raise ValueError("health.expected_pool_url cannot contain credentials")


@dataclass(frozen=True)
class RecoverySettings:
    enabled: bool = False
    consecutive_critical: int = 3
    cooldown_seconds: int = 900
    window_seconds: int = 3600
    max_reboots_per_window: int = 2
    state_file: str = "var/recovery-state.json"

    def __post_init__(self) -> None:
        _require_exact_type("recovery.enabled", self.enabled, bool)
        for name in ("consecutive_critical", "cooldown_seconds", "window_seconds", "max_reboots_per_window"):
            _require_exact_type(f"recovery.{name}", getattr(self, name), int)
        _require_exact_type("recovery.state_file", self.state_file, str)
        if self.consecutive_critical < 1:
            raise ValueError("recovery.consecutive_critical must be at least 1")
        if self.cooldown_seconds < 1 or self.window_seconds < self.cooldown_seconds:
            raise ValueError("recovery window must be at least as long as the cooldown")
        if self.max_reboots_per_window < 1:
            raise ValueError("recovery.max_reboots_per_window must be at least 1")
        if not self.state_file.strip():
            raise ValueError("recovery.state_file cannot be empty")


@dataclass(frozen=True)
class AuditSettings:
    log_file: str = "var/audit.jsonl"
    redaction_salt_env: str = "AVALON_Q_AUDIT_SALT"

    def __post_init__(self) -> None:
        _require_exact_type("audit.log_file", self.log_file, str)
        _require_exact_type("audit.redaction_salt_env", self.redaction_salt_env, str)
        if not self.log_file.strip():
            raise ValueError("audit.log_file cannot be empty")
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", self.redaction_salt_env):
            raise ValueError("audit.redaction_salt_env must be an environment variable name")


@dataclass(frozen=True)
class AppConfig:
    device: TcpSettings = field(default_factory=lambda: TcpSettings(host=""))
    health: HealthSettings = field(default_factory=HealthSettings)
    recovery: RecoverySettings = field(default_factory=RecoverySettings)
    audit: AuditSettings = field(default_factory=AuditSettings)


_TOP_LEVEL = {"device", "health", "recovery", "audit", "_notes"}
_FIELDS = {
    "device": set(TcpSettings.__dataclass_fields__),
    "health": set(HealthSettings.__dataclass_fields__),
    "recovery": set(RecoverySettings.__dataclass_fields__),
    "audit": set(AuditSettings.__dataclass_fields__),
}


def _validate_keys(data: Mapping[str, Any]) -> None:
    unknown_top = set(data) - _TOP_LEVEL
    if unknown_top:
        raise ValueError(f"unknown configuration section(s): {', '.join(sorted(unknown_top))}")
    for section_name, allowed in _FIELDS.items():
        section = data.get(section_name, {})
        if type(section) is not dict:
            raise ValueError(f"{section_name} must be a JSON object")
        unknown = set(section) - allowed
        if unknown:
            raise ValueError(f"unknown {section_name} setting(s): {', '.join(sorted(unknown))}")


def _validate_paths(config: AppConfig) -> AppConfig:
    raw_state_path = Path(config.recovery.state_file).expanduser()
    raw_audit_path = Path(config.audit.log_file).expanduser()
    paths = (
        raw_state_path.resolve(),
        raw_state_path.with_name(f"{raw_state_path.name}.lock").resolve(),
        raw_audit_path.resolve(),
        raw_audit_path.with_name(f"{raw_audit_path.name}.lock").resolve(),
    )
    if len(set(paths)) != len(paths):
        raise ValueError("audit and recovery state data/lock paths must be distinct")

    existing: list[tuple[Path, os.stat_result]] = []
    for candidate in paths:
        try:
            existing.append((candidate, candidate.stat()))
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError("audit and recovery paths could not be verified") from exc
    for index, (_first_path, first_stat) in enumerate(existing):
        for _second_path, second_stat in existing[index + 1 :]:
            if os.path.samestat(first_stat, second_stat):
                raise ValueError("audit and recovery state data/lock paths must be distinct")
    return config


def _bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def validate_live_host(host: str) -> None:
    validate_local_address(host)


def load_config(path: str | Path | None = None, environ: Mapping[str, str] | None = None) -> AppConfig:
    """Load JSON, then apply the documented environment overrides."""
    data: dict[str, Any] = {}
    if path:
        with Path(path).open("r", encoding="utf-8") as handle:
            loaded = json.load(
                handle,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_unique_object,
            )
        if type(loaded) is not dict:
            raise ValueError("configuration root must be a JSON object")
        data = loaded
    _validate_keys(data)

    config = AppConfig(
        device=TcpSettings(**{"host": "", **data.get("device", {})}),
        health=HealthSettings(**data.get("health", {})),
        recovery=RecoverySettings(**data.get("recovery", {})),
        audit=AuditSettings(**data.get("audit", {})),
    )

    env = os.environ if environ is None else environ
    device = config.device
    health = config.health
    recovery = config.recovery
    audit = config.audit
    if env.get("AVALON_Q_HOST"):
        device = replace(device, host=env["AVALON_Q_HOST"].strip())
    if env.get("AVALON_Q_PORT"):
        device = replace(device, port=int(env["AVALON_Q_PORT"]))
    if env.get("AVALON_Q_EXPECTED_HASHRATE_THS"):
        health = replace(health, expected_hashrate_ths=float(env["AVALON_Q_EXPECTED_HASHRATE_THS"]))
    if env.get("AVALON_Q_EXPECTED_POOL_URL"):
        health = replace(health, expected_pool_url=env["AVALON_Q_EXPECTED_POOL_URL"].strip())
    if env.get("AVALON_Q_RECOVERY_ENABLED"):
        recovery = replace(recovery, enabled=_bool(env["AVALON_Q_RECOVERY_ENABLED"]))
    if env.get("AVALON_Q_AUDIT_LOG"):
        audit = replace(audit, log_file=env["AVALON_Q_AUDIT_LOG"].strip())
    if env.get("AVALON_Q_STATE_FILE"):
        recovery = replace(recovery, state_file=env["AVALON_Q_STATE_FILE"].strip())
    return _validate_paths(replace(config, device=device, health=health, recovery=recovery, audit=audit))


def apply_cli_overrides(
    config: AppConfig,
    *,
    host: str | None = None,
    port: int | None = None,
    expected_hashrate_ths: float | None = None,
    expected_pool_url: str | None = None,
    audit_log: str | None = None,
) -> AppConfig:
    device = replace(config.device, **({"host": host} if host is not None else {}))
    if port is not None:
        device = replace(device, port=port)
    health = config.health
    if expected_hashrate_ths is not None:
        health = replace(health, expected_hashrate_ths=expected_hashrate_ths)
    if expected_pool_url is not None:
        health = replace(health, expected_pool_url=expected_pool_url)
    audit = replace(config.audit, **({"log_file": audit_log} if audit_log is not None else {}))
    return _validate_paths(replace(config, device=device, health=health, audit=audit))
