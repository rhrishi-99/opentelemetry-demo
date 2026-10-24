"""Command line entry point: python -m oncall.main <command> ..."""

import argparse
import json
import os
import sys
from datetime import UTC, datetime

from oncall.models import Alert


def _manual_alert(service: str | None, description: str) -> Alert:
    now = datetime.now(UTC)
    return {
        "id": f"manual-{now.strftime('%Y%m%dT%H%M%S')}",
        "ts": now.isoformat(),
        "service": service,
        "signal": "manual",
        "value": None,
        "threshold": None,
        "description": description,
        "source": "manual",
    }


def cmd_investigate(args: argparse.Namespace) -> int:
    if args.offline:
        os.environ["TELEMETRY_BACKEND"] = "offline"
        os.environ["OFFLINE_SCENARIO"] = args.offline

    from oncall.agent.loop import investigate_and_diagnose
    from oncall.agent.runlog import RunLog
    from oncall.config import load_settings
    from oncall.report.output import report_run

    runlog = RunLog(runs_dir=load_settings().RUNS_DIR)
    alert = _manual_alert(args.service, args.description)
    result = investigate_and_diagnose(alert, runlog=runlog)
    json_path, md_path = report_run(alert, result["diagnosis"], runlog)

    print(f"run_id:  {result['run_id']}")
    print(f"stopped: {result['stopped']} after {result['steps']} steps")
    print("tools:")
    for i, call in enumerate(runlog.events("tool_call"), 1):
        print(f"  {i}. {call['name']}({call['args']})")
    print(f"summary:\n{result['summary']}")
    print(f"diagnosis:\n{json.dumps(result['diagnosis'], indent=2)}")
    print(f"run log: {runlog.path}")
    print(f"report:  {json_path} + {md_path.name}")
    print(f"tokens:  {runlog.token_usage()}")
    return 0 if result["stopped"] in ("finish", "max_steps") else 1


def cmd_watch(args: argparse.Namespace) -> int:
    if args.offline:
        os.environ["TELEMETRY_BACKEND"] = "offline"
        os.environ["OFFLINE_SCENARIO"] = args.offline

    from oncall.agent.orchestrator import run_incident
    from oncall.config import load_settings
    from oncall.platform.alert import watch

    settings = load_settings()

    def on_check(entry: dict) -> None:
        found = entry.get("error") or f"{len(entry.get('alerts', []))} alert(s)"
        fired = f" -> fired {entry['fired']}" if "fired" in entry else ""
        if "quiet_until" in entry:
            fired += f" -> a human has it; quiet until {entry['quiet_until']}"
        if "suppressed" in entry:
            held = entry["suppressed"]
            fired = (
                f" (held by a human since run {held.get('run_id')}, "
                f"{held.get('status')}; quiet until {held.get('until')})"
            )
        print(f"[{entry['ts']}] {found}{fired}", flush=True)

    def on_alert(alert: Alert) -> dict:
        print(f"ALERT {alert['id']}: {alert['description']}", flush=True)
        # The full loop: detective -> planner -> human gate -> operator -> verify -> report.
        execution = run_incident(
            alert, channel=args.channel, dry_run=not args.execute, settings=settings
        )
        d = execution["plan"]["diagnosis_final"]
        print(
            f"  run {execution['investigation']['run_id']}: status={execution['status']}, "
            f"component={d['component']}, category={d['category']}, "
            f"confidence={d['confidence']:.2f}, decision={d['decision']}\n"
            f"  report:  {execution['report_path']}",
            flush=True,
        )
        return execution

    try:
        watch(poll_s=args.poll_s, on_alert=on_alert, once=args.once, on_check=on_check)
    except KeyboardInterrupt:
        print("watch stopped")
    return 0


def cmd_killswitch(args: argparse.Namespace) -> int:
    from oncall.safety import killswitch

    if args.state == "on":
        print(f"kill switch ENGAGED: {killswitch.engage()} (no action can run)")
    elif args.state == "off":
        was_on = killswitch.disengage()
        print("kill switch off" + ("" if was_on else " (it was not engaged)"))
    print(killswitch.status())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if args.offline:
        os.environ["TELEMETRY_BACKEND"] = "offline"
        os.environ["OFFLINE_SCENARIO"] = args.offline

    from oncall.agent.orchestrator import run_incident
    from oncall.config import load_settings

    settings = load_settings()
    alert = _manual_alert(args.service, args.description)
    execution = run_incident(
        alert,
        channel=args.channel,
        dry_run=not args.execute,
        settings=settings,
    )
    print(f"run_id:     {execution['investigation']['run_id']}")
    print(f"status:     {execution['status']}")
    print(f"report:     {execution['report_path']}")
    return 0 if execution["status"] in ("success", "no_action") else 1


HUMAN_DIRECTED = "human-directed action"


def cmd_act(args: argparse.Namespace) -> int:
    """A human picks the action; it still goes through the same gate (exact "yes"),
    executor (kill switch, allowlist, Action API, actions.jsonl) and verify. No LLM."""
    from pathlib import Path

    from oncall.agent.proposer import ACTION_RISK, ROLLBACK_HINT
    from oncall.agent.runlog import RunLog
    from oncall.config import load_settings
    from oncall.report.output import report_run
    from oncall.safety import executor
    from oncall.safety import verify as verify_mod

    settings = load_settings()
    run = RunLog(runs_dir=settings.RUNS_DIR)
    reason = args.reason or f"operator asked to {args.action} {args.target}"
    proposal = {
        "action": args.action,
        "target": args.target,
        "params": {"users": args.users} if args.users is not None else {},
        "reason": reason,
        "expected_effect": f"{args.target} recovers after {args.action}",
        "rollback_hint": ROLLBACK_HINT.get(
            args.action, "Escalate if it does not help."
        ),
        "risk": ACTION_RISK.get(args.action, "medium"),
        "source": "human",
    }
    alert = _manual_alert(args.target, f"{HUMAN_DIRECTED}: {args.action} {args.target}")
    alert["source"] = "human"
    diagnosis = {
        "component": args.target,
        "category": "unknown",
        "cause": f"No agent diagnosis: {reason}",
        "confidence": 0.0,
        "evidence_ids": [],
        "affected_services": [args.target],
        "alternatives": [],
        "decision": "act",
        "reasoning": f"{HUMAN_DIRECTED} (no LLM, no agent diagnosis): {reason}",
    }
    run.log("alert", alert=alert)
    run.log("handoff", **{"from": "human", "to": "human_gate", "proposal": proposal})
    result = executor.execute(
        proposal,  # type: ignore[arg-type]
        run,
        dry_run=not args.execute,
        diagnosis=diagnosis,  # type: ignore[arg-type]
        channel="cli",
        settings=settings,
    )
    verification = verify_mod.verify(alert, diagnosis, result, runlog=run)  # type: ignore[arg-type]
    json_path, _ = report_run(
        alert,
        diagnosis,  # type: ignore[arg-type]
        run,
        out_dir=Path(settings.RUNS_DIR) / "reports",
        actions=[result],
        verification=verification,
    )
    print(f"run_id:       {run.run_id}")
    print(f"action:       {args.action}({args.target}) dry_run={not args.execute}")
    print(f"result:       {result['detail']}")
    print(f"verification: {verification['outcome']}")
    print(f"report:       {json_path}")
    done = result["ok"] and verification["outcome"] in ("recovered", "skipped_dry_run")
    return 0 if done else 1


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m oncall.main")
    sub = parser.add_subparsers(dest="command", required=True)

    inv = sub.add_parser("investigate", help="investigate an alert")
    inv.add_argument("--service", default=None)
    inv.add_argument("--description", required=True)
    inv.add_argument("--offline", metavar="SCENARIO", default=None)
    inv.set_defaults(func=cmd_investigate)

    w = sub.add_parser("watch", help="watch for incidents and investigate them")
    w.add_argument("--once", action="store_true", help="run one check and exit")
    w.add_argument("--poll-s", type=int, default=None, help="seconds between checks")
    w.add_argument("--offline", metavar="SCENARIO", default=None)
    w.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="execute approved actions (default is dry-run)",
    )
    w.add_argument(
        "--channel", choices=["cli", "ui"], default="cli", help="approval channel"
    )
    w.set_defaults(func=cmd_watch)

    ks = sub.add_parser("killswitch", help="block every action (on), allow (off)")
    ks.add_argument("state", choices=["on", "off", "status"])
    ks.set_defaults(func=cmd_killswitch)
    a = sub.add_parser(
        "act", help="run one human-chosen action through the same gate and executor"
    )
    a.add_argument("--action", required=True, help="e.g. start_service")
    a.add_argument("--target", required=True, help="demo service, e.g. valkey-cart")
    a.add_argument("--reason", help="why (goes into the action log and report)")
    a.add_argument("--users", type=int, help="user count for throttle_load")
    a.add_argument(
        "--execute", action="store_true", help="really act (default is a dry run)"
    )
    a.set_defaults(func=cmd_act)
    r = sub.add_parser("run", help="run full remediation orchestrator on an incident")
    r.add_argument("--description", required=True, help="incident description")
    r.add_argument("--service", default=None, help="target service name")
    r.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="execute approved actions (default is dry-run)",
    )
    r.add_argument(
        "--channel",
        choices=["cli", "ui"],
        default="cli",
        help="approval channel (cli or ui)",
    )
    r.add_argument(
        "--offline",
        metavar="SCENARIO",
        default=None,
        help="run in offline mode with scenario fixture",
    )
    r.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
