"""Action API: the single allowlisted, audited executor for modifying the demo.

Per hackathon hard rules (AGENTS.md and RULES.md):
- Changes system state ONLY through this Action API.
- All actions are subject to strict in-code allowlisting.
- In dry-run mode, no changes are made to the live system.
- Every invocation (allowed, denied, dry, or real) is appended to runs/actions.jsonl.
- Feature flags are remediated strictly via a static runbook without exposing
  flag states or reading flag values.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from oncall.config import Settings, load_settings
from oncall.models import ActionRequest, ActionResult
from oncall.safety.actionlog import log_action
from oncall.safety.allowlist import validate_action

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FLAG_FILE = REPO_ROOT / "src" / "flagd" / "demo.flagd.json"
RUNBOOK_PATH = Path(__file__).resolve().parent / "flag_runbook.yaml"
LOCUST_BASE_URL = "http://127.0.0.1:8080/loadgen"
DEFAULT_VARIANT_PATTERN = re.compile(r'("defaultVariant"\s*:\s*)"[^"]*"')


def load_runbook(path: Path = RUNBOOK_PATH) -> dict[str, list[str]]:
    """Load the static service -> failure flag keys runbook."""
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def run_docker(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Execute a docker mutation command."""
    try:
        proc = subprocess.run(
            ["docker", *cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def inspect_container_state(target: str) -> dict[str, Any]:
    """Query a container's status via docker inspect without changing it."""
    code, stdout, _ = run_docker(["inspect", target, "--format", "{{json .State}}"])
    if code == 0 and stdout:
        try:
            state = json.loads(stdout)
            return {
                "status": state.get("Status", "unknown"),
                "running": bool(state.get("Running", False)),
                "exit_code": state.get("ExitCode", 0),
            }
        except (json.JSONDecodeError, KeyError, TypeError):
            return {"status": "unknown", "running": False}
    return {"status": "unknown", "running": False}


def apply_disable_flags(
    flags_to_disable: list[str],
    backup_file: Path,
    flag_file: Path = DEFAULT_FLAG_FILE,
) -> tuple[bool, str]:
    """Disable the given failure flags by setting defaultVariant to 'off'.

    Saves the prior file content as an opaque blob to backup_file for rollback.
    Does NOT return or report the previous or current flag values.
    """
    if not flag_file.is_file():
        return False, f"Flag configuration file not found at {flag_file}"

    try:
        raw_bytes = flag_file.read_bytes()
        backup_file.parent.mkdir(parents=True, exist_ok=True)
        backup_file.write_bytes(raw_bytes)

        text = raw_bytes.decode("utf-8")
        data = json.loads(text)
        flags_dict = data.get("flags", {})

        disabled_count = 0
        for key in flags_to_disable:
            if key in flags_dict:
                flags_dict[key]["defaultVariant"] = "off"
                disabled_count += 1

        updated_text = json.dumps(data, indent=2) + "\n"
        flag_file.write_text(updated_text, encoding="utf-8")
        return True, f"Disabled {disabled_count} failure flags in runbook"
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"Failed updating flags: {exc}"


def apply_restore_flags(
    backup_file: Path,
    flag_file: Path = DEFAULT_FLAG_FILE,
) -> tuple[bool, str]:
    """Restore the flag configuration from an opaque backup blob."""
    if not backup_file.is_file():
        return False, f"Backup file {backup_file} does not exist"

    try:
        raw_bytes = backup_file.read_bytes()
        flag_file.write_bytes(raw_bytes)
        return True, f"Restored flag file from backup {backup_file.name}"
    except OSError as exc:
        return False, f"Failed restoring flag backup: {exc}"


def get_locust_users(base_url: str = LOCUST_BASE_URL) -> int | None:
    """Fetch active Locust user count via Locust HTTP stats API."""
    try:
        url = f"{base_url.rstrip('/')}/stats/requests"
        resp = httpx.get(url, timeout=3.0)
        if resp.status_code == 200:
            data = resp.json()
            return int(data.get("user_count", 0))
    except (httpx.HTTPError, ValueError, json.JSONDecodeError):
        return None
    return None


def set_locust_users(users: int, base_url: str = LOCUST_BASE_URL) -> tuple[bool, str]:
    """Set the Locust swarm user count."""
    try:
        url = f"{base_url.rstrip('/')}/swarm"
        spawn_rate = max(1, users // 2)
        resp = httpx.post(
            url,
            data={"user_count": users, "spawn_rate": spawn_rate},
            timeout=5.0,
        )
        if resp.status_code in (200, 204):
            return True, f"Locust swarm set to {users} users"
        return False, f"Locust returned HTTP {resp.status_code}: {resp.text[:200]}"
    except (httpx.HTTPError, OSError) as exc:
        return False, f"Locust request failed: {exc}"


def execute(
    req: ActionRequest,
    settings: Settings | None = None,
    *,
    flag_file: Path | None = None,
    locust_url: str | None = None,
) -> ActionResult:
    """Execute an ActionRequest against the demo system.

    Never raises exceptions: errors are captured and returned in the ActionResult envelope.
    Appends the request and result to runs/actions.jsonl.
    """
    settings = settings or load_settings()
    action_id = f"act-{uuid.uuid4().hex[:8]}"
    ts = datetime.now(UTC).isoformat()

    runs_dir = Path(settings.RUNS_DIR)
    actions_log_path = runs_dir / "actions.jsonl"
    backup_dir = runs_dir / "action_backups"

    target_flag_file = flag_file or DEFAULT_FLAG_FILE
    target_locust_url = locust_url or LOCUST_BASE_URL

    is_dry_run = bool(req.get("dry_run", settings.DRY_RUN))

    allowed, detail, target, params = validate_action(req)
    if not allowed:
        result: ActionResult = {
            "request": req,
            "allowed": False,
            "executed": False,
            "dry_run": is_dry_run,
            "ok": False,
            "detail": detail,
            "before": {},
            "after": {},
            "rollback": None,
            "ts": ts,
        }
        log_action(result, action_id, actions_log_path)
        return result

    action = req.get("action", "")
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    rollback_req: ActionRequest | None = None
    ok = True
    executed = False

    try:
        if action == "restart_service":
            before = inspect_container_state(target)
            if is_dry_run:
                detail = f"[DRY RUN] Would restart container {target}"
            else:
                code, out, err = run_docker(["restart", target])
                ok = code == 0
                executed = True
                after = inspect_container_state(target)
                detail = (
                    f"Restarted container {target}"
                    if ok
                    else f"docker restart failed: {err or out}"
                )

        elif action == "start_service":
            before = inspect_container_state(target)
            if is_dry_run:
                detail = f"[DRY RUN] Would start container {target}"
            else:
                code, out, err = run_docker(["start", target])
                ok = code == 0
                executed = True
                after = inspect_container_state(target)
                detail = (
                    f"Started container {target}"
                    if ok
                    else f"docker start failed: {err or out}"
                )

        elif action == "disable_failure_flags":
            runbook = load_runbook()
            flags = runbook.get(target, [])
            backup_file = backup_dir / f"{action_id}.bak"
            before = {
                "backup_file": str(backup_file),
                "target_service": target,
                "flags_in_runbook": len(flags),
            }
            rollback_req = {
                "action": "restore_flags",
                "target": target,
                "params": {"backup_file": str(backup_file)},
                "reason": f"Rollback failure flags disabled on {target}",
                "run_id": req.get("run_id", ""),
                "dry_run": is_dry_run,
            }
            if is_dry_run:
                detail = (
                    f"[DRY RUN] Would disable failure flags for {target} "
                    f"({len(flags)} registered in runbook)"
                )
            else:
                ok, detail = apply_disable_flags(flags, backup_file, target_flag_file)
                executed = ok
                after = {"flags_disabled": len(flags)} if ok else {}

        elif action == "restore_flags":
            b_path = Path(params.get("backup_file") or params.get("backup_path") or "")
            before = {"backup_file": str(b_path)}
            if is_dry_run:
                detail = f"[DRY RUN] Would restore flags from backup {b_path.name}"
            else:
                ok, detail = apply_restore_flags(b_path, target_flag_file)
                executed = ok
                after = {"restored": True} if ok else {}

        elif action == "throttle_load":
            target_users = int(params["users"])
            current_users = get_locust_users(target_locust_url)
            prev_users = (
                current_users if current_users is not None else target_users * 2
            )
            before = {
                "users": current_users if current_users is not None else "unknown"
            }
            rollback_req = {
                "action": "throttle_load",
                "target": "load-generator",
                "params": {"users": prev_users},
                "reason": f"Rollback load throttling to {prev_users} users",
                "run_id": req.get("run_id", ""),
                "dry_run": is_dry_run,
            }
            if is_dry_run:
                detail = (
                    f"[DRY RUN] Would throttle load generator to {target_users} users "
                    f"(from {prev_users})"
                )
            else:
                ok, detail = set_locust_users(target_users, target_locust_url)
                executed = ok
                after = {"users": target_users} if ok else {}

    except (
        RuntimeError,
        ValueError,
        OSError,
        httpx.HTTPError,
        json.JSONDecodeError,
    ) as exc:
        ok = False
        detail = f"Action execution error: {exc}"

    result = {
        "request": req,
        "allowed": True,
        "executed": executed,
        "dry_run": is_dry_run,
        "ok": ok,
        "detail": detail,
        "before": before,
        "after": after,
        "rollback": rollback_req,
        "ts": ts,
    }
    log_action(result, action_id, actions_log_path)
    return result
