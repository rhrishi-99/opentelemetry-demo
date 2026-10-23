"""faultctl: inject and reset practice incidents on the local demo.

A human/test tool. The agent (oncall/) must never import or call it.
Only touches containers of the compose project opentelemetry-demo.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

PROJECT = "opentelemetry-demo"
HERE = Path(__file__).resolve().parent
SCENARIOS_PATH = HERE / "scenarios.yaml"
STATE_PATH = HERE / ".state.json"
TYPES = ("stop", "cpu_limit", "mem_limit")
# TODO(F18): "flag" scenarios once the flagd write mechanism is verified.
CPU_PERIOD = 100_000
RESET_WAIT_S = 120


class FaultctlError(Exception):
    pass


class Docker:
    def run(self, args: list[str]) -> str:
        proc = subprocess.run(
            ["docker", *args], check=False, capture_output=True, text=True, timeout=120
        )
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            raise FaultctlError(f"docker {' '.join(args)}: {detail}")
        return proc.stdout

    def project_containers(self) -> list[dict]:
        out = self.run(
            ["ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"]
        )
        ids = out.split()
        return json.loads(self.run(["inspect", *ids])) if ids else []

    def find(self, service: str) -> dict:
        for c in self.project_containers():
            if c["Config"]["Labels"].get("com.docker.compose.service") == service:
                return c
        raise FaultctlError(
            f"no container for service {service!r} in compose project {PROJECT}"
        )


def name_of(c: dict) -> str:
    return c["Name"].lstrip("/")


def snapshot(c: dict) -> dict:
    hc = c["HostConfig"]
    return {
        "running": bool(c["State"]["Running"]),
        "nano_cpus": hc.get("NanoCpus") or 0,
        "cpu_quota": hc.get("CpuQuota") or 0,
        "cpu_period": hc.get("CpuPeriod") or 0,
        "memory": hc.get("Memory") or 0,
        "memory_swap": hc.get("MemorySwap") or 0,
    }


def load_scenarios(path: Path = SCENARIOS_PATH) -> dict[str, dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    scenarios = data.get("scenarios") or {}
    for sid, sc in scenarios.items():
        if sc.get("type") not in TYPES:
            raise FaultctlError(f"{sid}: unsupported type {sc.get('type')!r}")
        if not sc.get("target"):
            raise FaultctlError(f"{sid}: missing target")
        params = sc.setdefault("params", {})
        if sc["type"] == "cpu_limit" and not float(params.get("cpus", 0)) > 0:
            raise FaultctlError(f"{sid}: cpu_limit needs params.cpus > 0")
        if sc["type"] == "mem_limit" and not params.get("memory"):
            raise FaultctlError(f"{sid}: mem_limit needs params.memory")
    return scenarios


def load_state(path: Path = STATE_PATH) -> dict:
    if not path.exists():
        return {"injected": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    if state["injected"]:
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    elif path.exists():
        path.unlink()


def apply(sc: dict, name: str, original: dict, docker: Docker) -> None:
    kind, params = sc["type"], sc["params"]
    if kind == "stop":
        docker.run(["stop", name])
    elif kind == "cpu_limit":
        cpus = float(params["cpus"])
        # A --cpus limit cannot be removed by docker update, so use quota/period
        # unless the container already has a --cpus limit (they conflict).
        if original["nano_cpus"]:
            docker.run(["update", "--cpus", str(cpus), name])
        else:
            quota = str(int(cpus * CPU_PERIOD))
            docker.run(
                ["update", "--cpu-period", str(CPU_PERIOD), "--cpu-quota", quota, name]
            )
    elif kind == "mem_limit":
        mem = str(params["memory"])
        docker.run(["update", "--memory", mem, "--memory-swap", mem, name])


def restore(entry: dict, docker: Docker) -> None:
    kind, name, o = entry["type"], entry["container"], entry["original"]
    if kind == "stop":
        if o["running"]:
            docker.run(["start", name])
    elif kind == "cpu_limit":
        if o["nano_cpus"]:
            docker.run(["update", "--cpus", str(o["nano_cpus"] / 1e9), name])
        else:
            args = ["update", "--cpu-quota", str(o["cpu_quota"] or -1)]
            if o["cpu_period"]:
                args += ["--cpu-period", str(o["cpu_period"])]
            docker.run([*args, name])
    elif kind == "mem_limit":
        if not o["memory"]:
            raise FaultctlError(
                f"{name}: had no memory limit; docker update cannot remove one. "
                f"Recreate it: docker compose up -d --force-recreate {entry['target']}"
            )
        docker.run(
            [
                "update",
                "--memory",
                str(o["memory"]),
                "--memory-swap",
                str(o["memory_swap"]),
                name,
            ]
        )


def inject(
    sid: str,
    docker: Docker,
    scenarios: dict[str, dict],
    state_path: Path = STATE_PATH,
) -> str:
    if sid not in scenarios:
        raise FaultctlError(f"unknown scenario {sid!r}; see `faultctl list`")
    state = load_state(state_path)
    if any(e["id"] == sid for e in state["injected"]):
        raise FaultctlError(f"{sid} is already injected; run `faultctl reset` first")
    sc = scenarios[sid]
    c = docker.find(sc["target"])
    name = name_of(c)
    entry = {
        "id": sid,
        "type": sc["type"],
        "target": sc["target"],
        "container": name,
        "params": sc["params"],
        "original": snapshot(c),
        "ts": datetime.now(UTC).isoformat(),
    }
    # Record before applying so a partial failure is still undone by reset.
    state["injected"].append(entry)
    save_state(state, state_path)
    apply(sc, name, entry["original"], docker)
    return f"injected {sid}: {sc['type']} on {name}"


def unhealthy(docker: Docker) -> list[str]:
    problems = []
    for c in docker.project_containers():
        st = c["State"]
        health = (st.get("Health") or {}).get("Status")
        if not st["Running"]:
            problems.append(f"{name_of(c)}: {st['Status']}")
        elif health in ("starting", "unhealthy"):
            problems.append(f"{name_of(c)}: {health}")
    return sorted(problems)


def wait_healthy(docker: Docker, wait_s: float, sleep=time.sleep) -> list[str]:
    deadline = time.monotonic() + wait_s
    while True:
        problems = unhealthy(docker)
        if not problems or time.monotonic() >= deadline:
            return problems
        sleep(5)


def reset(
    docker: Docker,
    state_path: Path = STATE_PATH,
    wait_s: float = RESET_WAIT_S,
    sleep=time.sleep,
) -> tuple[list[str], list[str]]:
    """Undo every injected fault, start exited project containers, verify.

    Returns (log lines, problems); no problems means the demo is healthy.
    """
    state = load_state(state_path)
    log, failed, problems = [], [], []
    for entry in reversed(state["injected"]):
        try:
            restore(entry, docker)
            log.append(f"restored {entry['id']} on {entry['container']}")
        except FaultctlError as exc:
            failed.append(entry)
            problems.append(f"restore {entry['id']}: {exc}")
    state["injected"] = list(reversed(failed))
    save_state(state, state_path)
    for c in docker.project_containers():
        if c["State"]["Status"] == "exited":
            docker.run(["start", name_of(c)])
            log.append(f"started exited container {name_of(c)}")
    problems += wait_healthy(docker, wait_s, sleep)
    return log, problems


def status(docker: Docker, state_path: Path = STATE_PATH) -> list[str]:
    state = load_state(state_path)
    if not state["injected"]:
        lines = ["nothing injected"]
    else:
        live = {name_of(c): c for c in docker.project_containers()}
        lines = []
        for e in state["injected"]:
            c = live.get(e["container"])
            now = snapshot(c) if c else None
            lines.append(
                f"{e['id']}: {e['type']} on {e['container']} since {e['ts']} "
                f"params={e['params']} now={now}"
            )
    lines += [f"not healthy: {p}" for p in unhealthy(docker)]
    return lines


def main(argv: list[str] | None = None, docker: Docker | None = None) -> int:
    parser = argparse.ArgumentParser(prog="faultctl", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p_inject = sub.add_parser("inject")
    p_inject.add_argument("scenario")
    sub.add_parser("status")
    p_reset = sub.add_parser("reset")
    p_reset.add_argument("--wait", type=float, default=RESET_WAIT_S)
    args = parser.parse_args(argv)
    docker = docker or Docker()
    try:
        if args.cmd == "list":
            for sid, sc in load_scenarios().items():
                print(
                    f"{sid}  {sc['type']:<10} {sc['target']:<16} {sc.get('description', '')}"
                )
        elif args.cmd == "inject":
            print(inject(args.scenario, docker, load_scenarios(), STATE_PATH))
        elif args.cmd == "status":
            print("\n".join(status(docker, STATE_PATH)))
        elif args.cmd == "reset":
            log, problems = reset(docker, STATE_PATH, wait_s=args.wait)
            print("\n".join(log or ["nothing to restore"]))
            if problems:
                print("\n".join(f"NOT HEALTHY: {p}" for p in problems))
                return 1
            print("all demo containers running and healthy")
    except FaultctlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
