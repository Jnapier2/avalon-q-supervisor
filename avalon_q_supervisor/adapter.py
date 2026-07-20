"""Canaan Avalon Q TCP 4028 adapter and telemetry normalization."""

from __future__ import annotations

import math
import re
import socket
import time
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urlsplit

from .models import DeviceSnapshot
from .network import validate_local_address


class AdapterError(RuntimeError):
    """Raised when a device response cannot be obtained safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TcpSettings:
    host: str
    port: int = 4028
    timeout_seconds: float = 3.0
    max_response_bytes: int = 1_048_576
    expected_model: str = "Q"

    def __post_init__(self) -> None:
        if type(self.host) is not str or any(character.isspace() for character in self.host):
            raise ValueError("host must be a whitespace-free string")
        if type(self.port) is not int or not 1 <= self.port <= 65_535:
            raise ValueError("port must be an integer between 1 and 65535")
        try:
            timeout_finite = type(self.timeout_seconds) in (int, float) and math.isfinite(
                self.timeout_seconds
            )
        except OverflowError:
            timeout_finite = False
        if not timeout_finite:
            raise ValueError("timeout_seconds must be finite")
        if not 0.1 <= self.timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 0.1 and 60")
        if type(self.max_response_bytes) is not int or not 1_024 <= self.max_response_bytes <= 8_388_608:
            raise ValueError("max_response_bytes must be an integer between 1024 and 8388608")
        if type(self.expected_model) is not str or not self.expected_model.strip() or len(self.expected_model) > 100:
            raise ValueError("expected_model must be a short non-empty string")


def _validate_command(command: str) -> None:
    if type(command) is not str or not command or len(command) > 1024:
        raise ValueError("command must contain 1 to 1024 characters")
    if any(character in command for character in ("\x00", "\r", "\n")):
        raise ValueError("command contains an invalid control character")


class CanaanTcpClient:
    """Small socket client for the documented plain-text command protocol."""

    def __init__(
        self,
        settings: TcpSettings,
        connection_factory: Callable[..., socket.socket] = socket.create_connection,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        validate_local_address(settings.host)
        self.settings = settings
        self._connection_factory = connection_factory
        self._monotonic = monotonic

    def request(self, command: str) -> str:
        _validate_command(command)
        deadline = self._monotonic() + self.settings.timeout_seconds
        chunks: list[bytes] = []
        terminated_by_nul = False
        reached_eof = False
        try:
            connection = self._connection_factory(
                (self.settings.host, self.settings.port),
                timeout=self.settings.timeout_seconds,
            )
            with connection:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise AdapterError("timeout", "device request exceeded its total deadline")
                connection.settimeout(remaining)
                connection.sendall(command.encode("ascii"))
                size = 0
                while True:
                    remaining = deadline - self._monotonic()
                    if remaining <= 0:
                        break
                    connection.settimeout(remaining)
                    try:
                        chunk = connection.recv(min(65_536, self.settings.max_response_bytes - size + 1))
                    except socket.timeout:
                        break
                    if not chunk:
                        reached_eof = True
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > self.settings.max_response_bytes:
                        raise AdapterError("response-too-large", "device response exceeded the configured limit")
                    if b"\x00" in chunk:
                        terminated_by_nul = True
                        break
        except AdapterError:
            raise
        except socket.timeout as exc:
            raise AdapterError("timeout", "device request timed out") from exc
        except OSError as exc:
            raise AdapterError("unreachable", "device connection failed") from exc

        try:
            response = b"".join(chunks).split(b"\x00", 1)[0].decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise AdapterError("invalid-response", "device response was not valid UTF-8") from exc
        if not response:
            code = "empty-response" if reached_eof or terminated_by_nul else "timeout"
            raise AdapterError(code, "device returned no complete data")
        try:
            complete = _is_complete_response(command, response)
        except ValueError as exc:
            raise AdapterError("invalid-response", "device response violated the protocol schema") from exc
        if not complete:
            raise AdapterError("incomplete-response", "device response was not a complete protocol frame")
        return response


def parse_legacy_response(response: str) -> list[dict[str, str]]:
    """Parse the pipe/comma response format documented for Avalon Q."""
    sections: list[dict[str, str]] = []
    for raw_section in response.replace("\x00", "").split("|"):
        if not raw_section.strip():
            continue
        fields: dict[str, str] = {}
        for index, raw_field in enumerate(raw_section.split(",")):
            field = raw_field.strip()
            if not field:
                continue
            if "=" in field:
                key, value = field.split("=", 1)
                key = key.strip()
                if key in fields:
                    raise ValueError(f"duplicate response field: {key}")
                fields[key] = value.strip().strip("'")
            elif index == 0:
                fields["section"] = field
        if fields:
            sections.append(fields)
    return sections


def _is_complete_response(command: str, response: str) -> bool:
    """Require a terminal delimiter and the section promised by the command."""
    if not response.endswith("|"):
        return False
    sections = parse_legacy_response(response)
    if not sections:
        return False
    status = _section(sections, "STATUS")
    if status is None or status.get("STATUS") not in {"S", "I", "W", "E", "F"}:
        return False
    if status.get("STATUS") in {"E", "F"}:
        return True
    command_name = command.split("|", 1)[0]
    expected_section = {
        "version": "VERSION",
        "summary": "SUMMARY",
        "estats": "STATS",
        "pools": "POOL",
    }.get(command_name)
    if expected_section is None:
        return command == AvalonQAdapter.REBOOT_COMMAND
    if _section(sections, expected_section) is not None:
        return True
    return command_name == "pools" and status.get("Msg", "").lstrip().startswith("0 Pool")


def _section(sections: Iterable[dict[str, str]], name: str) -> dict[str, str] | None:
    for section in sections:
        if name in section or section.get("section") == name:
            return section
    return None


def _number(value: str | None, converter: Callable[[float], float] = float) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
        return converter(number) if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _integer(value: str | None) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number.is_integer() and number >= 0 else None


def _bracket_number(text: str, key: str) -> float | None:
    match = re.search(rf"(?:^|[\s{{]){re.escape(key)}\[\s*(-?\d+(?:\.\d+)?)\s*\]", text)
    value = float(match.group(1)) if match else None
    return value if value is not None and math.isfinite(value) else None


def _bracket_integers(text: str, prefix: str) -> tuple[int, ...]:
    matches = re.findall(rf"(?:^|[\s{{]){re.escape(prefix)}\d+\[\s*(-?\d+)\s*\]", text)
    values: list[int] = []
    for value in matches:
        digits = value.removeprefix("-")
        if len(digits) > 10:
            return ()
        try:
            parsed = int(value)
        except ValueError:
            return ()
        if not 0 <= parsed <= 1_000_000:
            return ()
        values.append(parsed)
    return tuple(values)


def _normalize_url(value: str) -> tuple[str, int | None, str]:
    parsed = urlsplit(value.strip())
    return (parsed.hostname or "").lower(), parsed.port, parsed.scheme.lower()


def _pool_matches(actual: str, expected: str) -> bool:
    try:
        return _normalize_url(actual) == _normalize_url(expected)
    except ValueError:
        return actual.strip().lower().rstrip("/") == expected.strip().lower().rstrip("/")


def _pool_activity(section: dict[str, str]) -> bool | None:
    """Return an explicit activity state without turning malformed telemetry into failure."""
    stratum_active = section.get("Stratum Active")
    if stratum_active is not None:
        normalized = stratum_active.strip().casefold()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
        return None

    status = section.get("Status", "").strip().casefold()
    priority = section.get("Priority")
    if status == "alive" and priority is not None:
        parsed_priority = _integer(priority)
        return parsed_priority == 0 if parsed_priority is not None else None
    if status in {"dead", "disabled", "rejecting"}:
        return False
    return None


class AvalonQAdapter:
    """Read telemetry and issue the one bounded recovery command used by the supervisor."""

    QUERY_COMMANDS = ("version", "summary", "estats", "pools")
    REBOOT_COMMAND = "ascset|0,reboot,0"

    def __init__(self, client: CanaanTcpClient) -> None:
        self.client = client

    def query(self, command: str) -> list[dict[str, str]]:
        if command not in self.QUERY_COMMANDS:
            raise ValueError(f"unsupported read command: {command}")
        try:
            return parse_legacy_response(self.client.request(command))
        except ValueError as exc:
            raise AdapterError("invalid-response", "device response violated the protocol schema") from exc

    def reboot(self) -> list[dict[str, str]]:
        try:
            return parse_legacy_response(self.client.request(self.REBOOT_COMMAND))
        except ValueError as exc:
            raise AdapterError("invalid-response", "device response violated the protocol schema") from exc

    def verify_identity(self, expected_model: str) -> bool:
        """Recheck the documented model immediately before a state-changing command."""
        version = _section(self.query("version"), "VERSION") or {}
        candidates = {
            value.strip().casefold()
            for value in (version.get("MODEL"), version.get("PROD"))
            if value
        }
        return expected_model.strip().casefold() in candidates

    def poll(
        self,
        expected_pool_url: str = "",
        expected_model: str = "Q",
        observed_at: float | None = None,
    ) -> DeviceSnapshot:
        observed = time.time() if observed_at is None else observed_at
        try:
            summary_sections = self.query("summary")
        except AdapterError as exc:
            return DeviceSnapshot(observed_at=observed, reachable=False, error_code=exc.code)

        summary = _section(summary_sections, "SUMMARY") or {}
        optional: dict[str, list[dict[str, str]]] = {}
        for command in ("estats", "pools", "version"):
            try:
                optional[command] = self.query(command)
            except AdapterError:
                optional[command] = []

        estats_text = " ".join(
            " ".join(f"{key}={value}" for key, value in section.items())
            for section in optional["estats"]
        )
        version = _section(optional["version"], "VERSION") or {}
        pool_sections = [section for section in optional["pools"] if "POOL" in section]

        pool_states = [(section, _pool_activity(section)) for section in pool_sections]
        active_pools = [section for section, activity in pool_states if activity is True]
        if active_pools:
            pool_active: bool | None = True
        elif pool_states and all(activity is False for _section, activity in pool_states):
            pool_active = False
        else:
            pool_active = None

        hashrate_mhs = _number(summary.get("MHS 5s"))
        if hashrate_mhs is None:
            hashrate_mhs = _number(summary.get("MHS av"))
        hashrate_ths = hashrate_mhs / 1_000_000 if hashrate_mhs is not None else None
        if hashrate_ths is None:
            ghs = _bracket_number(estats_text, "GHSavg")
            if ghs is None:
                ghs = _bracket_number(estats_text, "GHSspd")
            hashrate_ths = ghs / 1_000 if ghs is not None else None
        if hashrate_ths is not None and hashrate_ths < 0:
            hashrate_ths = None

        temperature = _bracket_number(estats_text, "TMax")
        if temperature is None:
            candidates = [
                value
                for key in ("ITemp", "HBITemp", "HBOTemp", "TAvg")
                if (value := _bracket_number(estats_text, key)) is not None
            ]
            temperature = max(candidates) if candidates else None
        if temperature is not None and not -100 <= temperature <= 200:
            temperature = None

        pool_matches_expected: bool | None = None
        if expected_pool_url and active_pools:
            pool_matches_expected = any(
                _pool_matches(section.get("URL", ""), expected_pool_url) for section in active_pools
            )

        identity_candidates = [
            value.strip()
            for value in (version.get("MODEL"), version.get("PROD"))
            if value and len(value.strip()) <= 100
        ]
        model = identity_candidates[0] if identity_candidates else None
        identity_verified: bool | None = None
        if identity_candidates:
            candidates = {value.casefold() for value in identity_candidates}
            identity_verified = expected_model.strip().casefold() in candidates

        return DeviceSnapshot(
            observed_at=observed,
            reachable=True,
            hashrate_ths=hashrate_ths,
            temperature_c=temperature,
            fan_rpm=_bracket_integers(estats_text, "Fan"),
            accepted_shares=_integer(summary.get("Accepted")),
            rejected_shares=_integer(summary.get("Rejected")),
            elapsed_seconds=_integer(summary.get("Elapsed")),
            pool_active=pool_active,
            pool_matches_expected=pool_matches_expected,
            model=model,
            identity_verified=identity_verified,
        )
