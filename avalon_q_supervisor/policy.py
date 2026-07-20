"""Process-atomic restart budgets and cooldown decisions."""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from .config import RecoverySettings
from .health import HealthAssessment, HealthState
from .locking import LockTimeoutError, exclusive_file_lock


class RecoveryAction(str, Enum):
    NONE = "none"
    OBSERVE = "observe"
    WOULD_REBOOT = "would-reboot"
    REBOOT = "reboot"
    SUPPRESSED_DISABLED = "suppressed-disabled"
    SUPPRESSED_COOLDOWN = "suppressed-cooldown"
    SUPPRESSED_BUDGET = "suppressed-budget"
    SUPPRESSED_STATE = "suppressed-state"
    SUPPRESSED_AUDIT = "suppressed-audit"
    MANUAL_INTERVENTION = "manual-intervention"


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction
    reason: str
    critical_streak: int


@dataclass
class RecoveryState:
    critical_streak: int = 0
    condition_key: str = ""
    attempt_times: list[float] = field(default_factory=list)
    last_attempt_at: float | None = None
    last_plan_at: float | None = None


class StateUnavailableError(OSError):
    """Raised internally when durable recovery state cannot be trusted."""


_STATE_KEYS = {
    "schema_version",
    "critical_streak",
    "condition_key",
    "attempt_times",
    "last_attempt_at",
    "last_plan_at",
}


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate recovery-state key: {key}")
        result[key] = value
    return result


def _finite_timestamp(name: str, value: object, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a number")
    try:
        finite = math.isfinite(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not finite or value < 0:
        raise ValueError(f"{name} must be a nonnegative finite number")
    return float(value)


def _state_from_document(data: object) -> RecoveryState:
    if type(data) is not dict:
        raise ValueError("recovery state root must be an object")
    if set(data) != _STATE_KEYS:
        raise ValueError("recovery state schema does not match this version")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported recovery state schema")
    if type(data["critical_streak"]) is not int or data["critical_streak"] < 0:
        raise ValueError("critical_streak must be a nonnegative integer")
    if type(data["condition_key"]) is not str or len(data["condition_key"]) > 1000:
        raise ValueError("condition_key must be a bounded string")
    if type(data["attempt_times"]) is not list or len(data["attempt_times"]) > 10_000:
        raise ValueError("attempt_times must be a bounded array")
    attempt_times = [_finite_timestamp("attempt_times item", value) for value in data["attempt_times"]]
    condition_key = data["condition_key"]
    critical_streak = data["critical_streak"]
    if (critical_streak == 0) != (condition_key == ""):
        raise ValueError("critical streak and condition key are inconsistent")
    verified_attempts = [value for value in attempt_times if value is not None]
    if verified_attempts != sorted(verified_attempts):
        raise ValueError("attempt_times must be sorted")
    last_attempt = _finite_timestamp("last_attempt_at", data["last_attempt_at"], optional=True)
    if verified_attempts and last_attempt != verified_attempts[-1]:
        raise ValueError("last_attempt_at must match the latest retained attempt")
    return RecoveryState(
        critical_streak=critical_streak,
        condition_key=condition_key,
        attempt_times=verified_attempts,
        last_attempt_at=last_attempt,
        last_plan_at=_finite_timestamp("last_plan_at", data["last_plan_at"], optional=True),
    )


class RecoveryPolicy:
    """Reload, decide, and reserve under one bounded exclusive lock."""

    def __init__(
        self,
        settings: RecoverySettings,
        state_path: str | Path | None = None,
        *,
        lock_timeout_seconds: float = 5.0,
    ) -> None:
        self.settings = settings
        self.state_path = Path(state_path) if state_path else None
        self.lock_path = (
            self.state_path.with_name(f"{self.state_path.name}.lock") if self.state_path else None
        )
        if (
            type(lock_timeout_seconds) not in (int, float)
            or not math.isfinite(lock_timeout_seconds)
            or not 0.1 <= lock_timeout_seconds <= 60
        ):
            raise ValueError("lock timeout must be finite and between 0.1 and 60 seconds")
        self.lock_timeout_seconds = lock_timeout_seconds
        self._memory_lock = threading.RLock()
        self._memory_state = RecoveryState()

    def _lock(self):
        if self.lock_path is None:
            return self._memory_lock
        return exclusive_file_lock(self.lock_path, timeout_seconds=self.lock_timeout_seconds)

    def _load_locked(self) -> RecoveryState:
        if self.state_path is None:
            return RecoveryState(**asdict(self._memory_state))
        try:
            self.state_path.lstat()
        except FileNotFoundError:
            return RecoveryState()
        except OSError as exc:
            raise StateUnavailableError("recovery state path cannot be inspected") from exc
        try:
            if self.state_path.stat().st_size > 1_048_576:
                raise ValueError("recovery state exceeds the size limit")
            with self.state_path.open("r", encoding="utf-8") as handle:
                document = json.load(
                    handle,
                    parse_constant=_reject_json_constant,
                    object_pairs_hook=_unique_object,
                )
            return _state_from_document(document)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateUnavailableError("recovery state is unreadable or invalid") from exc

    def _save_locked(self, state: RecoveryState) -> None:
        if self.state_path is None:
            self._memory_state = RecoveryState(**asdict(state))
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.state_path.name}.", suffix=".tmp", dir=self.state_path.parent
            )
            try:
                document = {"schema_version": 1, **asdict(state)}
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(
                        document,
                        handle,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, self.state_path)
                if os.name != "nt":
                    directory_descriptor = os.open(self.state_path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_descriptor)
                    finally:
                        os.close(directory_descriptor)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        except (OSError, TypeError, ValueError) as exc:
            raise StateUnavailableError("recovery state could not be persisted") from exc

    def _prune(self, state: RecoveryState, now: float) -> None:
        cutoff = now - self.settings.window_seconds
        state.attempt_times = [value for value in state.attempt_times if value >= cutoff]

    @staticmethod
    def _reset_streak(state: RecoveryState) -> None:
        state.critical_streak = 0
        state.condition_key = ""

    def _decide(
        self,
        state: RecoveryState,
        assessment: HealthAssessment,
        *,
        now: float,
        execute_requested: bool,
    ) -> RecoveryDecision:
        self._prune(state, now)
        if assessment.state is not HealthState.CRITICAL:
            self._reset_streak(state)
            return RecoveryDecision(RecoveryAction.NONE, "state-not-recoverable", 0)
        if not assessment.recoverable:
            self._reset_streak(state)
            return RecoveryDecision(RecoveryAction.MANUAL_INTERVENTION, "unsafe-for-automatic-reboot", 0)

        condition_key = "|".join(sorted(assessment.reasons))
        if condition_key == state.condition_key:
            state.critical_streak = min(
                state.critical_streak + 1,
                self.settings.consecutive_critical,
            )
        else:
            state.condition_key = condition_key
            state.critical_streak = 1

        if state.critical_streak < self.settings.consecutive_critical:
            return RecoveryDecision(
                RecoveryAction.OBSERVE,
                "waiting-for-consecutive-evidence",
                state.critical_streak,
            )
        if not self.settings.enabled:
            return RecoveryDecision(
                RecoveryAction.SUPPRESSED_DISABLED,
                "recovery-disabled-by-configuration",
                state.critical_streak,
            )
        if state.last_attempt_at is not None and now - state.last_attempt_at < self.settings.cooldown_seconds:
            return RecoveryDecision(
                RecoveryAction.SUPPRESSED_COOLDOWN,
                "reboot-cooldown-active",
                state.critical_streak,
            )
        if len(state.attempt_times) >= self.settings.max_reboots_per_window:
            return RecoveryDecision(
                RecoveryAction.SUPPRESSED_BUDGET,
                "rolling-reboot-budget-exhausted",
                state.critical_streak,
            )
        if not execute_requested:
            if state.last_plan_at is not None and now - state.last_plan_at < self.settings.cooldown_seconds:
                return RecoveryDecision(
                    RecoveryAction.SUPPRESSED_COOLDOWN,
                    "dry-run-plan-cooldown-active",
                    state.critical_streak,
                )
            state.last_plan_at = now
            self._reset_streak(state)
            return RecoveryDecision(RecoveryAction.WOULD_REBOOT, "dry-run-only", self.settings.consecutive_critical)
        return RecoveryDecision(RecoveryAction.REBOOT, "bounded-recovery-approved", state.critical_streak)

    def decide_and_reserve(
        self,
        assessment: HealthAssessment,
        *,
        now: float,
        execute_requested: bool,
        before_reserve: Callable[[RecoveryDecision], None] | None = None,
    ) -> RecoveryDecision:
        """Atomically reload limits, write intent, and reserve before returning REBOOT."""
        try:
            valid_now = type(now) in (int, float) and math.isfinite(now) and now >= 0
        except OverflowError:
            valid_now = False
        if not valid_now or type(execute_requested) is not bool:
            return RecoveryDecision(RecoveryAction.SUPPRESSED_STATE, "invalid-recovery-input", 0)
        try:
            with self._lock():
                state = self._load_locked()
                decision = self._decide(
                    state,
                    assessment,
                    now=now,
                    execute_requested=execute_requested,
                )
                if decision.action is RecoveryAction.REBOOT:
                    if before_reserve is None:
                        self._save_locked(state)
                        return RecoveryDecision(
                            RecoveryAction.SUPPRESSED_AUDIT,
                            "recovery-intent-writer-required",
                            decision.critical_streak,
                        )
                    before_reserve(decision)
                    state.attempt_times.append(now)
                    state.last_attempt_at = now
                    self._reset_streak(state)
                self._save_locked(state)
                return decision
        except (LockTimeoutError, StateUnavailableError, OSError):
            return RecoveryDecision(RecoveryAction.SUPPRESSED_STATE, "recovery-state-unavailable", 0)

    def read_state(self) -> RecoveryState:
        """Return a verified snapshot for diagnostics and tests."""
        try:
            with self._lock():
                return self._load_locked()
        except (LockTimeoutError, StateUnavailableError, OSError) as exc:
            raise StateUnavailableError(str(exc)) from exc
