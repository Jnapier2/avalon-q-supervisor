"""One-cycle orchestration: observe, classify, decide, act, and audit."""

from __future__ import annotations

from dataclasses import dataclass

from .adapter import AdapterError, AvalonQAdapter
from .audit import JsonlAuditLogger
from .health import HealthAssessment, HealthClassifier
from .models import DeviceSnapshot
from .policy import RecoveryAction, RecoveryDecision, RecoveryPolicy


@dataclass(frozen=True)
class CycleResult:
    snapshot: DeviceSnapshot
    assessment: HealthAssessment
    decision: RecoveryDecision
    command_outcome: str


class RecoveryIntentAuditError(RuntimeError):
    """Raised when recovery intent cannot be durably recorded."""


class AvalonQSupervisor:
    def __init__(
        self,
        *,
        adapter: AvalonQAdapter | None,
        classifier: HealthClassifier,
        policy: RecoveryPolicy,
        audit: JsonlAuditLogger,
        device_ref: str,
        expected_pool_url: str = "",
        expected_model: str = "Q",
        execute_requested: bool = False,
    ) -> None:
        self.adapter = adapter
        self.classifier = classifier
        self.policy = policy
        self.audit = audit
        self.device_ref = device_ref
        self.expected_pool_url = expected_pool_url
        self.expected_model = expected_model
        self.execute_requested = execute_requested

    def run_once(
        self,
        *,
        snapshot: DeviceSnapshot | None = None,
        now: float | None = None,
    ) -> CycleResult:
        if snapshot is None:
            if self.adapter is None:
                raise ValueError("an adapter is required for a live observation")
            snapshot = self.adapter.poll(
                expected_pool_url=self.expected_pool_url,
                expected_model=self.expected_model,
                observed_at=now,
            )
        decision_time = snapshot.observed_at if now is None else now
        assessment = self.classifier.assess(snapshot)

        def write_recovery_intent(decision: RecoveryDecision) -> None:
            try:
                self.audit.write(
                    {
                        "event": "recovery-intent",
                        "device_ref": self.device_ref,
                        "health": {
                            "state": assessment.state.value,
                            "reasons": list(assessment.reasons),
                            "recoverable": assessment.recoverable,
                        },
                        "metrics": snapshot.audit_metrics(),
                        "recovery": {
                            "action": decision.action.value,
                            "reason": decision.reason,
                            "critical_streak": decision.critical_streak,
                            "execute_requested": self.execute_requested,
                            "phase": "pending-reservation",
                        },
                    },
                    observed_at=snapshot.observed_at,
                )
            except (OSError, TypeError, ValueError) as exc:
                raise RecoveryIntentAuditError("recovery intent could not be persisted") from exc

        try:
            decision = self.policy.decide_and_reserve(
                assessment,
                now=decision_time,
                execute_requested=self.execute_requested,
                before_reserve=write_recovery_intent,
            )
        except RecoveryIntentAuditError:
            decision = RecoveryDecision(
                RecoveryAction.SUPPRESSED_AUDIT,
                "recovery-intent-audit-failed",
                0,
            )
            return CycleResult(snapshot, assessment, decision, "blocked-audit-failure")

        command_outcome = "not-requested"
        if decision.action is RecoveryAction.REBOOT:
            if self.adapter is None:
                command_outcome = "blocked-no-adapter"
            else:
                try:
                    identity_matches = self.adapter.verify_identity(self.expected_model)
                except AdapterError as exc:
                    command_outcome = f"blocked-identity-recheck-{exc.code}"
                else:
                    if not identity_matches:
                        command_outcome = "blocked-identity-recheck"
                    else:
                        try:
                            response = self.adapter.reboot()
                        except AdapterError as exc:
                            command_outcome = f"command-unconfirmed-{exc.code}"
                        else:
                            status = response[0].get("STATUS", "") if response else ""
                            command_outcome = (
                                "accepted" if status in {"S", "I"} else "response-unconfirmed"
                            )
        elif decision.action is RecoveryAction.WOULD_REBOOT:
            command_outcome = "dry-run"
        elif decision.action is RecoveryAction.SUPPRESSED_STATE:
            command_outcome = "blocked-state-unavailable"
        elif decision.action is RecoveryAction.SUPPRESSED_AUDIT:
            command_outcome = "blocked-audit-failure"

        self.audit.write(
            {
                "event": "recovery-outcome" if decision.action is RecoveryAction.REBOOT else "supervision-cycle",
                "device_ref": self.device_ref,
                "health": {
                    "state": assessment.state.value,
                    "reasons": list(assessment.reasons),
                    "recoverable": assessment.recoverable,
                },
                "metrics": snapshot.audit_metrics(),
                "recovery": {
                    "action": decision.action.value,
                    "reason": decision.reason,
                    "critical_streak": decision.critical_streak,
                    "execute_requested": self.execute_requested,
                    "command_outcome": command_outcome,
                },
            },
            observed_at=snapshot.observed_at,
        )
        return CycleResult(snapshot, assessment, decision, command_outcome)
