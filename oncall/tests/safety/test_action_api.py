"""Unit tests for Action API (allowlist, dry run, execution, backup & log)."""

import json
from pathlib import Path
from unittest import mock

import pytest

from oncall.config import Settings
from oncall.models import ActionRequest
from oncall.safety.action_api import (
    DEFAULT_FLAG_FILE,
    RUNBOOK_PATH,
    execute,
    load_runbook,
)
from oncall.safety.actionlog import read_action_log
from oncall.safety.allowlist import DEMO_SERVICES, validate_action


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    runs_dir = tmp_path / "runs"
    return Settings(RUNS_DIR=str(runs_dir), DRY_RUN=False)


@pytest.fixture
def sample_flag_file(tmp_path: Path) -> Path:
    flags_data = {
        "$schema": "https://flagd.dev/schema/v0/flags.json",
        "flags": {
            "paymentFailure": {
                "defaultVariant": "50%",
                "state": "ENABLED",
                "variants": {"off": 0, "50%": 0.5},
            },
            "paymentUnreachable": {
                "defaultVariant": "on",
                "state": "ENABLED",
                "variants": {"off": False, "on": True},
            },
            "cartFailure": {
                "defaultVariant": "25%",
                "state": "ENABLED",
                "variants": {"off": 0, "25%": 0.25},
            },
        },
    }
    p = tmp_path / "test_demo.flagd.json"
    p.write_text(json.dumps(flags_data, indent=2), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Allowlist validation tests
# ---------------------------------------------------------------------------


def test_allowlist_permits_known_demo_services():
    assert "cart" in DEMO_SERVICES
    assert "payment" in DEMO_SERVICES
    assert "valkey-cart" in DEMO_SERVICES

    req: ActionRequest = {
        "action": "restart_service",
        "target": "cartservice",  # alias
        "params": {},
        "reason": "restart cart",
        "run_id": "r1",
        "dry_run": False,
    }
    allowed, _, target, _ = validate_action(req)
    assert allowed is True
    assert target == "cart"


def test_allowlist_rejects_disallowed_and_system_containers():
    for forbidden in [
        "flagd",
        "flagd-ui",
        "prometheus",
        "jaeger",
        "opensearch",
        "frontend-proxy",
    ]:
        req: ActionRequest = {
            "action": "restart_service",
            "target": forbidden,
            "params": {},
            "reason": "restart",
            "run_id": "r1",
            "dry_run": False,
        }
        allowed, detail, _, _ = validate_action(req)
        assert allowed is False
        assert "disallowed" in detail or "not an allowlisted" in detail


def test_allowlist_rejects_scale_service_on_compose():
    req: ActionRequest = {
        "action": "scale_service",
        "target": "cart",
        "params": {"replicas": 3},
        "reason": "scale up",
        "run_id": "r1",
        "dry_run": False,
    }
    allowed, detail, _, _ = validate_action(req)
    assert allowed is False
    assert "not supported on Docker Compose" in detail


def test_allowlist_rejects_unknown_action():
    req: ActionRequest = {
        "action": "arbitrary_command",
        "target": "cart",
        "params": {},
        "reason": "hack",
        "run_id": "r1",
        "dry_run": False,
    }
    allowed, detail, _, _ = validate_action(req)
    assert allowed is False
    assert "not in allowlist" in detail


def test_allowlist_validates_throttle_load_params():
    # Valid
    req1: ActionRequest = {
        "action": "throttle_load",
        "target": "load-generator",
        "params": {"users": 5},
        "reason": "reduce load",
        "run_id": "r1",
        "dry_run": False,
    }
    assert validate_action(req1)[0] is True

    # Invalid target
    req2: ActionRequest = {
        "action": "throttle_load",
        "target": "frontend",
        "params": {"users": 5},
        "reason": "reduce load",
        "run_id": "r1",
        "dry_run": False,
    }
    assert validate_action(req2)[0] is False

    # Invalid users count (< 1 or non-int)
    req3: ActionRequest = {
        "action": "throttle_load",
        "target": "load-generator",
        "params": {"users": 0},
        "reason": "reduce load",
        "run_id": "r1",
        "dry_run": False,
    }
    assert validate_action(req3)[0] is False


# ---------------------------------------------------------------------------
# Dry-run tests
# ---------------------------------------------------------------------------


def test_dry_run_executes_nothing(tmp_settings: Settings, sample_flag_file: Path):
    req: ActionRequest = {
        "action": "restart_service",
        "target": "cart",
        "params": {},
        "reason": "restart cart",
        "run_id": "dry-1",
        "dry_run": True,
    }

    with mock.patch("oncall.safety.action_api.run_docker") as mock_docker:
        # Mock container inspect returning state for "before"
        mock_docker.return_value = (0, '{"Status": "restarting", "Running": false}', "")
        res = execute(req, tmp_settings)

    assert res["allowed"] is True
    assert res["dry_run"] is True
    assert res["executed"] is False
    assert res["ok"] is True
    assert "[DRY RUN]" in res["detail"]
    # Verify no restart command was dispatched
    for call in mock_docker.call_args_list:
        assert "restart" not in call[0][0]


def test_dry_run_disable_flags_prepares_rollback(
    tmp_settings: Settings, sample_flag_file: Path
):
    req: ActionRequest = {
        "action": "disable_failure_flags",
        "target": "payment",
        "params": {},
        "reason": "disable payment failure flags",
        "run_id": "dry-flags-1",
        "dry_run": True,
    }

    res = execute(req, tmp_settings, flag_file=sample_flag_file)
    assert res["allowed"] is True
    assert res["dry_run"] is True
    assert res["executed"] is False
    assert res["rollback"] is not None
    assert res["rollback"]["action"] == "restore_flags"
    # Ensure sample flag file was not altered in dry-run
    data = json.loads(sample_flag_file.read_text(encoding="utf-8"))
    assert data["flags"]["paymentFailure"]["defaultVariant"] == "50%"


# ---------------------------------------------------------------------------
# Real execution tests
# ---------------------------------------------------------------------------


def test_restart_service_real_execution(tmp_settings: Settings):
    req: ActionRequest = {
        "action": "restart_service",
        "target": "cart",
        "params": {},
        "reason": "restart cart",
        "run_id": "exec-1",
        "dry_run": False,
    }

    with mock.patch("oncall.safety.action_api.run_docker") as mock_docker:
        mock_docker.side_effect = [
            (0, '{"Status": "running", "Running": true}', ""),  # inspect before
            (0, "cart", ""),  # restart
            (0, '{"Status": "running", "Running": true}', ""),  # inspect after
        ]
        res = execute(req, tmp_settings)

    assert res["allowed"] is True
    assert res["executed"] is True
    assert res["ok"] is True
    assert res["rollback"] is None
    assert "Restarted container cart" in res["detail"]


def test_start_service_real_execution(tmp_settings: Settings):
    req: ActionRequest = {
        "action": "start_service",
        "target": "valkey-cart",
        "params": {},
        "reason": "start stopped valkey",
        "run_id": "exec-start-1",
        "dry_run": False,
    }

    with mock.patch("oncall.safety.action_api.run_docker") as mock_docker:
        mock_docker.side_effect = [
            (0, '{"Status": "exited", "Running": false}', ""),  # inspect before
            (0, "valkey-cart", ""),  # start
            (0, '{"Status": "running", "Running": true}', ""),  # inspect after
        ]
        res = execute(req, tmp_settings)

    assert res["allowed"] is True
    assert res["executed"] is True
    assert res["ok"] is True
    assert "Started container valkey-cart" in res["detail"]


def test_disable_failure_flags_and_rollback(
    tmp_settings: Settings, sample_flag_file: Path
):
    req: ActionRequest = {
        "action": "disable_failure_flags",
        "target": "payment",
        "params": {},
        "reason": "turn off payment failure flags",
        "run_id": "flag-run-1",
        "dry_run": False,
    }

    # 1. Execute disable_failure_flags
    res = execute(req, tmp_settings, flag_file=sample_flag_file)
    assert res["allowed"] is True
    assert res["executed"] is True
    assert res["ok"] is True
    assert res["rollback"] is not None

    # Check file modification: flags set to "off"
    data = json.loads(sample_flag_file.read_text(encoding="utf-8"))
    assert data["flags"]["paymentFailure"]["defaultVariant"] == "off"
    assert data["flags"]["paymentUnreachable"]["defaultVariant"] == "off"
    # Unrelated flags unchanged
    assert data["flags"]["cartFailure"]["defaultVariant"] == "25%"

    # Ensure no flag values were reported in before/after/detail
    assert "50%" not in str(res)
    assert "defaultVariant" not in str(res)

    # 2. Execute the ready-made rollback request
    rollback_req = res["rollback"]
    res_rollback = execute(rollback_req, tmp_settings, flag_file=sample_flag_file)
    assert res_rollback["allowed"] is True
    assert res_rollback["executed"] is True
    assert res_rollback["ok"] is True

    # Check that original flag file bytes were restored
    restored_data = json.loads(sample_flag_file.read_text(encoding="utf-8"))
    assert restored_data["flags"]["paymentFailure"]["defaultVariant"] == "50%"
    assert restored_data["flags"]["paymentUnreachable"]["defaultVariant"] == "on"


def test_throttle_load_and_rollback(tmp_settings: Settings):
    req: ActionRequest = {
        "action": "throttle_load",
        "target": "load-generator",
        "params": {"users": 5},
        "reason": "throttle load",
        "run_id": "load-1",
        "dry_run": False,
    }

    with (
        mock.patch("oncall.safety.action_api.get_locust_users", return_value=20),
        mock.patch(
            "oncall.safety.action_api.set_locust_users",
            return_value=(True, "Swarm updated"),
        ) as mock_set,
    ):
        res = execute(req, tmp_settings)

    assert res["allowed"] is True
    assert res["executed"] is True
    assert res["ok"] is True
    assert res["before"]["users"] == 20
    assert res["after"]["users"] == 5
    assert res["rollback"] is not None
    assert res["rollback"]["params"]["users"] == 20
    mock_set.assert_called_once_with(5, mock.ANY)


# ---------------------------------------------------------------------------
# Audit log and runbook structure tests
# ---------------------------------------------------------------------------


def test_actions_jsonl_append_only(tmp_settings: Settings):
    runs_dir = Path(tmp_settings.RUNS_DIR)
    log_file = runs_dir / "actions.jsonl"

    req1: ActionRequest = {
        "action": "restart_service",
        "target": "cart",
        "params": {},
        "reason": "reason 1",
        "run_id": "run-a",
        "dry_run": True,
    }
    req2: ActionRequest = {
        "action": "scale_service",
        "target": "cart",
        "params": {},
        "reason": "reason 2",
        "run_id": "run-b",
        "dry_run": True,
    }

    execute(req1, tmp_settings)
    execute(req2, tmp_settings)

    assert log_file.is_file()
    lines = [
        json.loads(line) for line in log_file.read_text().splitlines() if line.strip()
    ]
    assert len(lines) == 2
    assert lines[0]["run_id"] == "run-a"
    assert lines[0]["allowed"] is True
    assert lines[1]["run_id"] == "run-b"
    assert lines[1]["allowed"] is False

    # Test reader helper
    assert len(read_action_log(log_file, run_id="run-a")) == 1
    assert len(read_action_log(log_file, run_id="run-b")) == 1


def test_flag_runbook_keys_match_demo_schema():
    assert RUNBOOK_PATH.is_file()
    runbook = load_runbook(RUNBOOK_PATH)
    assert "payment" in runbook
    assert "cart" in runbook
    assert "ad" in runbook

    # Verify that all flags in flag_runbook exist in default flag file if present
    if DEFAULT_FLAG_FILE.is_file():
        flag_data = json.loads(DEFAULT_FLAG_FILE.read_text(encoding="utf-8"))
        available_flags = set(flag_data.get("flags", {}).keys())
        for service, flags in runbook.items():
            for f in flags:
                assert f in available_flags, (
                    f"Runbook flag {f!r} for {service} not in demo flags"
                )
