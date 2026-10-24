import json
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest

from oncall.config import Settings
from oncall.platform import alert
from oncall.platform.alert import (
    Debouncer,
    capture_baseline,
    check_once,
    merge_alerts,
    watch,
)
from oncall.safety.exclusions import EXCLUDED_SERVICES
from oncall.tools.backends import BackendError

NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
SETTINGS = Settings(ALERT_ERROR_RATE=0.05, ALERT_P95_MS=1000, ALERT_POLL_S=15)


def _vector(values: dict[str, float]) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {"service_name": s}, "value": [1761220800, str(v)]}
                for s, v in values.items()
            ],
        },
    }


def _matrix(values: dict[str, list[float]]) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"service_name": s},
                    "values": [[1761220800 + 60 * i, str(v)] for i, v in enumerate(vs)],
                }
                for s, vs in values.items()
            ],
        },
    }


class FakeBackend:
    """Answers the error-rate and p95 queries with canned per-service values."""

    def __init__(self, errors=None, p95=None, base_errors=None, base_p95=None):
        self.errors = errors or {}
        self.p95 = p95 or {}
        self.base_errors = base_errors or {}
        self.base_p95 = base_p95 or {}
        self.queries: list[str] = []

    def prom_query(self, promql, at=None):
        self.queries.append(promql)
        return _vector(self.errors if "STATUS_CODE_ERROR" in promql else self.p95)

    def prom_range(self, promql, start, end, step="30s"):
        if "STATUS_CODE_ERROR" in promql:
            return _matrix(self.base_errors)
        return _matrix(self.base_p95)


@pytest.fixture(autouse=True)
def no_docker(monkeypatch):
    """Metric tests run without Docker; container tests patch this back in."""
    monkeypatch.delenv("ALERT_IGNORE_SERVICES", raising=False)
    monkeypatch.setattr(alert, "_container_alerts", lambda *args: [])


def test_healthy_demo_raises_nothing():
    backend = FakeBackend(errors={"cart": 0.0, "checkout": 0.01}, p95={"cart": 40.0})
    assert check_once(backend, {}, SETTINGS, NOW) == []


def test_uses_get_metrics_promql_with_5m_window():
    backend = FakeBackend()
    check_once(backend, {}, SETTINGS, NOW)
    assert all("[5m]" in q for q in backend.queries)
    assert any("traces_span_metrics_calls_total" in q for q in backend.queries)


def test_error_rate_over_fixed_threshold():
    backend = FakeBackend(errors={"payment": 0.40, "cart": 0.01})
    [a] = check_once(backend, {}, SETTINGS, NOW)
    assert a["service"] == "payment"
    assert a["signal"] == "error_rate"
    assert a["value"] == 0.40
    assert a["threshold"] == 0.05
    assert a["source"] == "detector"
    assert a["description"] == (
        "payment error rate is 40.0% over the last 5m (threshold 5.0%)."
    )


def test_baseline_raises_the_threshold_for_noisy_services():
    baseline = {"error_rate": {"ad": 0.04}}  # normally 4% errors -> threshold 12%
    assert check_once(FakeBackend(errors={"ad": 0.10}), baseline, SETTINGS, NOW) == []
    [a] = check_once(FakeBackend(errors={"ad": 0.13}), baseline, SETTINGS, NOW)
    assert a["threshold"] == pytest.approx(0.12)


def test_latency_over_threshold():
    baseline = {"p95_ms": {"recommendation": 800.0}}  # -> threshold 1600 ms
    backend = FakeBackend(p95={"recommendation": 1700.0, "cart": 1200.0})
    alerts = check_once(backend, baseline, SETTINGS, NOW)
    assert [(a["service"], a["signal"]) for a in alerts] == [
        ("cart", "latency"),
        ("recommendation", "latency"),
    ]


def test_edge_service_errors_are_a_symptom_alert_without_a_service():
    backend = FakeBackend(errors={"frontend-proxy": 0.20}, p95={"load-generator": 9e3})
    [a] = check_once(backend, {}, SETTINGS, NOW)
    assert a["service"] is None
    assert a["description"].startswith(
        "User-facing symptom: error rate at frontend-proxy"
    )


def test_ignored_services_never_alert(monkeypatch):
    monkeypatch.setenv("ALERT_IGNORE_SERVICES", "quote, email")
    backend = FakeBackend(errors={"quote": 0.9, "email": 0.9, "cart": 0.9})
    assert [a["service"] for a in check_once(backend, {}, SETTINGS, NOW)] == ["cart"]


def test_prometheus_down_raises():
    backend = mock.Mock()
    backend.prom_query.side_effect = BackendError("unreachable")
    with pytest.raises(BackendError):
        check_once(backend, {}, SETTINGS, NOW)


def test_capture_baseline_takes_the_median():
    backend = FakeBackend(
        base_errors={"cart": [0.0, 0.01, 0.5]},  # one spike does not move the median
        base_p95={"cart": [40.0, 50.0, 60.0, float("nan")]},
    )
    baseline = capture_baseline(backend, NOW)
    assert baseline == {"error_rate": {"cart": 0.01}, "p95_ms": {"cart": 50.0}}


def _inspect(name, status="running", oom=False, exit_code=0):
    return {
        "Name": f"/{name}",
        "RestartCount": 0,
        "State": {
            "Status": status,
            "OOMKilled": oom,
            "ExitCode": exit_code,
            "StartedAt": "2026-10-23T09:00:00Z",
        },
        "Config": {"Labels": {"com.docker.compose.service": name}},
    }


def test_container_down_raises_container_alert(monkeypatch):
    monkeypatch.undo()  # use the real container check
    monkeypatch.delenv("ALERT_IGNORE_SERVICES", raising=False)
    infos = [
        _inspect("cart", status="exited", exit_code=137),
        _inspect("load-generator", status="exited"),  # edge: not alerted
        _inspect("payment"),
    ]
    with (
        mock.patch.object(
            alert.containers, "compose_container_ids", return_value=["x"]
        ),
        mock.patch.object(alert.containers, "inspect_containers", return_value=infos),
    ):
        alerts = check_once(FakeBackend(), {}, SETTINGS, NOW)
    assert [(a["service"], a["signal"]) for a in alerts] == [("cart", "container")]
    assert "not running (state=exited, exit_code=137)" in alerts[0]["description"]


def test_docker_down_only_skips_the_container_check(monkeypatch):
    monkeypatch.undo()
    with mock.patch.object(
        alert.containers,
        "compose_container_ids",
        side_effect=alert.containers.DockerError("daemon down"),
    ):
        alerts = check_once(FakeBackend(errors={"cart": 0.5}), {}, SETTINGS, NOW)
    assert [a["service"] for a in alerts] == ["cart"]


def _a(service, signal, value=None, threshold=None):
    return alert._alert(NOW, service, signal, value, threshold, f"{service} {signal}.")


def test_merge_prefers_named_service_and_clearest_signal():
    alerts = [
        _a(None, "error_rate", 0.5, 0.05),
        _a("checkout", "error_rate", 0.2, 0.05),
        _a("payment", "error_rate", 0.9, 0.05),
        _a("cart", "container"),
    ]
    merged = merge_alerts(alerts)
    assert merged["service"] == "cart"
    assert merged["description"] == (
        "cart container. Also seen: payment error_rate. checkout error_rate. "
        "None error_rate."
    )


def test_debounce_one_alert_per_incident():
    d = Debouncer(suppress_min=10)
    firing = [_a("cart", "error_rate", 0.5, 0.05)]
    assert d.should_fire(firing, NOW) is True
    assert d.should_fire(firing, NOW + timedelta(minutes=1)) is False
    assert d.should_fire(firing, NOW + timedelta(minutes=9)) is False
    assert d.should_fire(firing, NOW + timedelta(minutes=11)) is True


def test_debounce_resets_when_signals_recover():
    d = Debouncer(suppress_min=10)
    firing = [_a("cart", "error_rate", 0.5, 0.05)]
    assert d.should_fire(firing, NOW) is True
    assert d.should_fire([], NOW + timedelta(minutes=1)) is False
    assert d.should_fire(firing, NOW + timedelta(minutes=2)) is True


class _Stop(Exception):
    pass


def _sleep_n_times(n):
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        if len(calls) >= n:
            raise _Stop

    return sleep, calls


def test_watch_fires_once_per_incident_and_logs_every_check(tmp_path):
    settings = Settings(RUNS_DIR=str(tmp_path), ALERT_POLL_S=15)
    fired = []
    sleep, calls = _sleep_n_times(3)
    with pytest.raises(_Stop):
        watch(
            on_alert=fired.append,
            backend=FakeBackend(errors={"payment": 0.4}),
            settings=settings,
            sleep=sleep,
        )
    assert [a["service"] for a in fired] == ["payment"]  # 3 checks, 1 alert
    assert calls == [15, 15, 15]
    lines = [json.loads(x) for x in (tmp_path / "watch.jsonl").read_text().splitlines()]
    assert [x["event"] for x in lines] == ["baseline", "check", "check", "check"]
    assert "fired" in lines[1]
    assert "fired" not in lines[2]


def test_watch_once_on_healthy_demo(tmp_path):
    settings = Settings(RUNS_DIR=str(tmp_path))
    fired = []
    watch(on_alert=fired.append, once=True, backend=FakeBackend(), settings=settings)
    assert fired == []
    last = json.loads((tmp_path / "watch.jsonl").read_text().splitlines()[-1])
    assert last["alerts"] == []


def test_watch_survives_prometheus_outage_and_on_alert_errors(tmp_path):
    settings = Settings(RUNS_DIR=str(tmp_path))
    backend = mock.Mock()
    backend.prom_range.side_effect = BackendError("down")
    backend.prom_query.side_effect = BackendError("down")
    watch(on_alert=lambda a: None, once=True, backend=backend, settings=settings)
    last = json.loads((tmp_path / "watch.jsonl").read_text().splitlines()[-1])
    assert last["error"] == "down"

    def broken(a):
        raise RuntimeError("agent crashed")

    watch(
        on_alert=broken,
        once=True,
        backend=FakeBackend(errors={"cart": 0.9}),
        settings=settings,
    )
    last = json.loads((tmp_path / "watch.jsonl").read_text().splitlines()[-1])
    assert last["on_alert_error"] == "RuntimeError: agent crashed"


def test_browser_errors_are_a_symptom_alert():
    [a] = check_once(FakeBackend(errors={"frontend-web": 0.3}), {}, SETTINGS, NOW)
    assert a["service"] is None


def test_failing_healthcheck_alone_is_not_an_alert(monkeypatch):
    monkeypatch.undo()
    slow = _inspect("checkout")
    slow["State"]["Health"] = {"Status": "unhealthy"}
    infos = [slow, _inspect("cart", status="exited", exit_code=137)]
    with (
        mock.patch.object(
            alert.containers, "compose_container_ids", return_value=["x"]
        ),
        mock.patch.object(alert.containers, "inspect_containers", return_value=infos),
    ):
        alerts = check_once(FakeBackend(), {}, SETTINGS, NOW)
    assert [a["service"] for a in alerts] == ["cart"]


def test_watch_passes_every_check_to_on_check(tmp_path):
    seen = []
    watch(
        once=True,
        backend=FakeBackend(errors={"cart": 0.9}),
        settings=Settings(RUNS_DIR=str(tmp_path)),
        on_alert=lambda a: None,
        on_check=seen.append,
    )
    assert len(seen) == 1
    assert seen[0]["fired"].endswith("-cart-error_rate")


def test_cli_watch_hands_alerts_to_the_agent(monkeypatch, tmp_path, capsys):
    from oncall import main as cli

    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    diagnosis = {
        "component": "cart",
        "category": "container_down",
        "confidence": 0.8,
        "decision": "act",
    }
    result = {"run_id": "r1", "stopped": "finish", "diagnosis": diagnosis}

    def fake_watch(poll_s, on_alert, once, on_check):
        assert once is True
        on_alert(_a("cart", "container"))

    with (
        mock.patch("oncall.platform.alert.watch", side_effect=fake_watch),
        mock.patch(
            "oncall.agent.loop.investigate_and_diagnose", return_value=result
        ) as agent,
        mock.patch(
            "oncall.report.output.report_run",
            return_value=(tmp_path / "r1.json", tmp_path / "r1.md"),
        ) as report,
    ):
        assert cli.main(["watch", "--once"]) == 0
    assert agent.call_args.args[0]["service"] == "cart"
    assert report.call_args.args[1] == diagnosis
    out = capsys.readouterr().out
    assert "ALERT" in out
    assert "report:" in out
    assert "component=cart" in out
    assert "decision=act" in out


def test_service_already_failing_at_start_still_alerts():
    # Live: recommendation was at 100% errors when watch started; a baseline of
    # 1.0 must not raise its threshold to 3.0 and hide it.
    baseline = {"error_rate": {"recommendation": 1.0}}
    backend = FakeBackend(errors={"recommendation": 1.0})
    [a] = check_once(backend, baseline, SETTINGS, NOW)
    assert a["service"] == "recommendation"
    assert a["threshold"] == 0.05


def test_debounce_fires_again_for_a_new_failing_service():
    d = Debouncer(suppress_min=10)
    rec = _a("recommendation", "error_rate", 1.0, 0.05)
    cart = _a("cart", "container")
    assert d.should_fire([rec], NOW) is True
    assert d.should_fire([rec], NOW + timedelta(minutes=1)) is False
    assert d.should_fire([rec, cart], NOW + timedelta(minutes=2)) is True
    assert d.should_fire([rec, cart], NOW + timedelta(minutes=3)) is False


def test_knock_on_errors_during_an_incident_do_not_re_alert():
    # Live: cart stopped -> alert; frontend errors 30 s later are the same incident.
    d = Debouncer(suppress_min=10)
    cart = _a("cart", "container")
    frontend = _a("frontend", "error_rate", 0.15, 0.05)
    assert d.should_fire([cart], NOW) is True
    assert d.should_fire([cart, frontend], NOW + timedelta(seconds=30)) is False


def test_edge_baseline_still_raises_its_threshold():
    # Live: load-generator runs at ~28% errors normally; that is not an alert.
    baseline = {"error_rate": {"load-generator": 0.28}}
    backend = FakeBackend(errors={"load-generator": 0.30})
    assert check_once(backend, baseline, SETTINGS, NOW) == []


def test_shared_exclusion_list_never_alerts():
    errors = dict.fromkeys(EXCLUDED_SERVICES, 0.9)
    errors["cart"] = 0.9
    alerts = check_once(FakeBackend(errors=errors), {}, SETTINGS, NOW)
    assert [a["service"] for a in alerts] == ["cart"]


def test_queries_carry_the_shared_exclusions():
    backend = FakeBackend()
    check_once(backend, {}, SETTINGS, NOW)
    assert all("{exclude}" not in q for q in backend.queries)
    assert all('service_name!~"' in q for q in backend.queries)
