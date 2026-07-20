"""Append-only JSONL audit events with identifier redaction."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .locking import exclusive_file_lock


_SENSITIVE_KEYS = re.compile(
    r"(?:host|ip|mac|dna|wallet|worker|user|password|passwd|credential|secret|token|authorization)",
    re.IGNORECASE,
)
_MAC = re.compile(r"\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b")
_BITCOIN = re.compile(r"\b(?:bc1[ac-hj-np-z02-9]{20,}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_BRACKETED_IPV6 = re.compile(
    r"\[(?P<address>[0-9a-fA-F:.]+)(?:%[A-Za-z0-9_.~-]+)?\](?::\d{1,5})?"
)
_UNBRACKETED_IPV6 = re.compile(
    r"(?<![0-9a-fA-F:.])"
    r"(?P<address>(?:[0-9a-fA-F]{0,4}:){2,7}"
    r"(?:(?:\d{1,3}\.){3}\d{1,3}|[0-9a-fA-F]{0,4}))"
    r"(?:%[A-Za-z0-9_.~-]+)?"
    r"(?![0-9a-fA-F:.])"
)


def device_reference(host: str, salt: str | None) -> str:
    """Return a stable pseudonym only when the operator supplies a private salt."""
    if not salt:
        return "device-local"
    digest = hashlib.sha256(f"{salt}\x00{host}".encode("utf-8")).hexdigest()[:12]
    return f"device-{digest}"


def _redact_text(value: str) -> str:
    redacted = _MAC.sub("[REDACTED-MAC]", value)
    redacted = _BITCOIN.sub("[REDACTED-WALLET]", redacted)

    def replace_bracketed_ipv6(match: re.Match[str]) -> str:
        candidate = match.group("address")
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            return match.group(0)
        return "[REDACTED-IPV6]" if address.version == 6 else match.group(0)

    redacted = _BRACKETED_IPV6.sub(replace_bracketed_ipv6, redacted)

    def replace_unbracketed_ipv6(match: re.Match[str]) -> str:
        try:
            address = ipaddress.ip_address(match.group("address"))
        except ValueError:
            return match.group(0)
        return "[REDACTED-IPV6]" if address.version == 6 else match.group(0)

    redacted = _UNBRACKETED_IPV6.sub(replace_unbracketed_ipv6, redacted)

    def replace_ipv4(match: re.Match[str]) -> str:
        try:
            ipaddress.ip_address(match.group(0))
        except ValueError:
            return match.group(0)
        return "[REDACTED-IP]"

    redacted = _IPV4.sub(replace_ipv4, redacted)
    tokens = re.split(r"([\s,;|]+)", redacted)
    for index, token in enumerate(tokens):
        try:
            ipaddress.ip_address(token.strip("[]()"))
        except ValueError:
            continue
        tokens[index] = "[REDACTED-IP]"
    return "".join(tokens)[:1000]


def sanitize(value: Any, key: str = "") -> Any:
    if _SENSITIVE_KEYS.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(item_key): sanitize(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value[:100]]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value))


def _fsync_parent_directory(path: Path) -> None:
    """Persist a newly created audit directory entry where POSIX supports it."""
    if os.name == "nt":
        return
    directory_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


class JsonlAuditLogger:
    """Write bounded, one-event-per-line operational evidence."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")

    def write(self, event: Mapping[str, Any], *, observed_at: float | None = None) -> dict[str, Any]:
        timestamp = observed_at
        if timestamp is None:
            timestamp = datetime.now(tz=timezone.utc).timestamp()
        try:
            valid_timestamp = type(timestamp) in (int, float) and math.isfinite(timestamp) and timestamp >= 0
        except OverflowError:
            valid_timestamp = False
        if not valid_timestamp:
            raise ValueError("audit timestamp must be a nonnegative finite number")
        try:
            timestamp_text = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except (OSError, OverflowError, ValueError) as exc:
            raise ValueError("audit timestamp is outside the supported range") from exc
        sanitized_event = sanitize(dict(event))
        sanitized_event.pop("schema_version", None)
        sanitized_event.pop("timestamp", None)
        document = {
            **sanitized_event,
            "schema_version": 1,
            "timestamp": timestamp_text,
        }
        payload = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        if len(payload.encode("utf-8")) > 32_768:
            raise ValueError("audit event exceeds the 32 KiB limit")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.lock_path, timeout_seconds=5.0):
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_parent_directory(self.path)
        return document
