"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

from .adapter import AvalonQAdapter, CanaanTcpClient
from .audit import JsonlAuditLogger, device_reference
from .config import AppConfig, apply_cli_overrides, load_config, validate_live_host
from .health import HealthClassifier
from .models import snapshot_from_mapping
from .policy import RecoveryPolicy
from .supervisor import AvalonQSupervisor, CycleResult


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite fixture value is not allowed: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate fixture key is not allowed: {key}")
        result[key] = value
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="avalon-q-supervisor",
        description="Local-first health classification and bounded recovery for Avalon Q.",
    )
    parser.add_argument("--config", help="path to JSON configuration")
    parser.add_argument("--host", help="device host; overrides JSON and AVALON_Q_HOST")
    parser.add_argument("--port", type=int, help="device TCP port")
    parser.add_argument("--expected-hashrate-ths", type=float, help="expected device rate in TH/s")
    parser.add_argument("--expected-pool-url", help="optional endpoint used only for a mismatch check")
    parser.add_argument("--audit-log", help="JSONL audit path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check", help="run one live observation")
    watch = subparsers.add_parser("watch", help="observe on a fixed interval")
    watch.add_argument("--interval", type=float, default=30.0, help="seconds between observations")
    watch.add_argument("--cycles", type=int, default=0, help="stop after N cycles; 0 runs until interrupted")

    demo = subparsers.add_parser("demo", help="run deterministic fixture observations without hardware")
    demo.add_argument("--fixture", default="fixtures/demo_sequence.json", help="normalized JSON fixture")

    for command in (subparsers.choices["check"], watch):
        command.add_argument(
            "--execute",
            action="store_true",
            help="permit recovery commands when configuration also enables recovery",
        )
    return parser


def _load(args: argparse.Namespace) -> AppConfig:
    config = load_config(args.config)
    return apply_cli_overrides(
        config,
        host=args.host,
        port=args.port,
        expected_hashrate_ths=args.expected_hashrate_ths,
        expected_pool_url=args.expected_pool_url,
        audit_log=args.audit_log,
    )


def _supervisor(config: AppConfig, execute: bool, *, live: bool) -> AvalonQSupervisor:
    salt = os.environ.get(config.audit.redaction_salt_env, "")
    adapter = AvalonQAdapter(CanaanTcpClient(config.device)) if live else None
    state_path = config.recovery.state_file if live else None
    return AvalonQSupervisor(
        adapter=adapter,
        classifier=HealthClassifier(config.health),
        policy=RecoveryPolicy(config.recovery, state_path=state_path),
        audit=JsonlAuditLogger(config.audit.log_file),
        device_ref=device_reference(config.device.host, salt),
        expected_pool_url=config.health.expected_pool_url,
        expected_model=config.device.expected_model,
        execute_requested=execute,
    )


def _print_result(result: CycleResult) -> None:
    output = {
        "timestamp": result.snapshot.observed_at,
        "health": result.assessment.state.value,
        "reasons": list(result.assessment.reasons),
        "recovery": result.decision.action.value,
        "outcome": result.command_outcome,
    }
    print(json.dumps(output, sort_keys=True))


def _run_demo(args: argparse.Namespace, config: AppConfig) -> int:
    with Path(args.fixture).open("r", encoding="utf-8") as handle:
        observations = json.load(
            handle,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_object,
        )
    if type(observations) is not list or not observations:
        raise ValueError("demo fixture must be a non-empty JSON array")
    # Demo mode can illustrate the recovery gate but can never send a command.
    demo_config = replace(config, recovery=replace(config.recovery, enabled=True))
    supervisor = _supervisor(demo_config, execute=False, live=False)
    for item in observations:
        snapshot = snapshot_from_mapping(item)
        _print_result(supervisor.run_once(snapshot=snapshot, now=snapshot.observed_at))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = _load(args)
        if args.command == "demo":
            return _run_demo(args, config)
        if not config.device.host:
            raise ValueError("a device host is required for live commands")
        validate_live_host(config.device.host)
        if args.command == "watch":
            if not math.isfinite(args.interval) or not 5 <= args.interval <= 86_400:
                raise ValueError("watch interval must be finite and between 5 and 86400 seconds")
            if args.cycles < 0:
                raise ValueError("watch cycles cannot be negative")

        supervisor = _supervisor(config, execute=args.execute, live=True)
        cycles = 1 if args.command == "check" else args.cycles
        completed = 0
        while cycles == 0 or completed < cycles:
            _print_result(supervisor.run_once(now=time.time()))
            completed += 1
            if cycles == 0 or completed < cycles:
                time.sleep(args.interval)
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
