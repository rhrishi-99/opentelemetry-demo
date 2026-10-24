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

    from oncall.agent.loop import investigate_and_diagnose
    from oncall.agent.runlog import RunLog
    from oncall.config import load_settings
    from oncall.platform.alert import watch
    from oncall.report.output import report_run

    def on_check(entry: dict) -> None:
        found = entry.get("error") or f"{len(entry.get('alerts', []))} alert(s)"
        fired = f" -> fired {entry['fired']}" if "fired" in entry else ""
        print(f"[{entry['ts']}] {found}{fired}", flush=True)

    def on_alert(alert: Alert) -> None:
        print(f"ALERT {alert['id']}: {alert['description']}", flush=True)
        runlog = RunLog(runs_dir=load_settings().RUNS_DIR)
        result = investigate_and_diagnose(alert, runlog=runlog)
        json_path, md_path = report_run(alert, result["diagnosis"], runlog)
        d = result["diagnosis"]
        print(
            f"  run {result['run_id']}: stopped={result['stopped']}, "
            f"component={d['component']}, category={d['category']}, "
            f"confidence={d['confidence']:.2f}, decision={d['decision']}\n"
            f"  run log: {runlog.path}\n"
            f"  report:  {json_path} + {md_path.name}",
            flush=True,
        )

    try:
        watch(poll_s=args.poll_s, on_alert=on_alert, once=args.once, on_check=on_check)
    except KeyboardInterrupt:
        print("watch stopped")
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
    w.set_defaults(func=cmd_watch)

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
