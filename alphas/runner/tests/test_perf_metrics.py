"""Tests for bounded performance observations exposed by alpha-runner."""

from __future__ import annotations

from runner.metrics import RunnerMetrics
from runner.perf_metrics import InvalidMetricCapacity, LabeledLatencyWindows, LatencyWindow
from runner.shared_panel_feature_cache import SharedPanelFeatureCache


def test_latency_window_keeps_bounded_recent_samples_and_total_count() -> None:
    window = LatencyWindow(max_samples=3)

    for value in (1.0, 2.0, 3.0, 4.0, 5.0):
        window.observe(value)

    assert window.snapshot() == {
        "count_total": 5,
        "sample_count": 3,
        "avg": 4.0,
        "max": 5.0,
        "p50": 4.0,
        "p95": 5.0,
        "p99": 5.0,
    }


def test_labeled_latency_windows_bound_metric_cardinality() -> None:
    windows = LabeledLatencyWindows(max_labels=2, max_samples=4)

    windows.observe("15m:first", 1.0)
    windows.observe("1h:second", 2.0)
    windows.observe("4h:third", 3.0)

    assert list(windows.snapshot()) == ["1h:second", "4h:third"]


def test_latency_window_rejects_non_positive_capacity_with_typed_error() -> None:
    try:
        LatencyWindow(max_samples=0)
    except InvalidMetricCapacity as exc:
        assert exc.field == "max_samples"
        assert exc.value == 0
    else:
        raise AssertionError("LatencyWindow accepted a zero-sized sample window")


def test_runner_metrics_expose_queue_semaphore_and_scan_quantiles() -> None:
    metrics = RunnerMetrics()

    metrics.inc("pubsub_connection_error_total")
    metrics.scan_wait_started()
    metrics.scan_wait_finished()
    metrics.observe_event(
        kind="symbols",
        queue_wait_ms=3.0,
        semaphore_wait_ms=4.0,
        scan_ms=5.0,
        total_ms=6.0,
        scanned=True,
    )
    metrics.observe_event(
        kind="kline",
        queue_wait_ms=1.0,
        semaphore_wait_ms=2.0,
        scan_ms=0.0,
        total_ms=3.0,
        scanned=False,
    )

    performance = metrics.snapshot()["performance"]
    assert metrics.snapshot()["pubsub_connection_error_total"] == 1
    assert performance["event_total"] == 2
    assert performance["scan_total"] == 1
    assert performance["event_by_kind"] == {"symbols": 1, "kline": 1}
    assert performance["scan_waiters_current"] == 0
    assert performance["scan_waiters_max"] == 1
    assert performance["queue_wait_ms"]["p95"] == 3.0
    assert performance["semaphore_wait_ms"]["p95"] == 4.0
    assert performance["scan_ms"]["p95"] == 5.0
    assert performance["scan_ms"]["count_total"] == 1
    assert performance["total_ms"]["p95"] == 6.0


def test_panel_cache_exposes_bounded_build_and_selection_latency() -> None:
    cache = SharedPanelFeatureCache()

    cache.observe_seconds("selection_compute_duration_sec_total", 0.25)
    cache.observe_panel_build("15m", "universe-a", 0.5)

    latency = cache.snapshot()["latency"]
    assert latency["selection_compute_sec"]["p95"] == 0.25
    assert latency["panel_build_sec"]["p95"] == 0.5
    assert latency["panel_build_by_group_sec"]["15m:universe-a"]["p95"] == 0.5


def test_reset_alpha_event_forgets_last_event_timestamp() -> None:
    """A re-claimed alpha must not inherit a stale last-event timestamp,
    otherwise /health flags it stale before it processes its next candle."""
    metrics = RunnerMetrics()
    metrics.mark_event_processed("alpha-1h", 1000.0)
    assert metrics.last_event_ts_by_alpha["alpha-1h"] == 1000.0

    metrics.reset_alpha_event("alpha-1h")
    assert "alpha-1h" not in metrics.last_event_ts_by_alpha


def test_reset_alpha_event_missing_alpha_is_noop() -> None:
    metrics = RunnerMetrics()
    metrics.reset_alpha_event("never-seen")
    assert metrics.last_event_ts_by_alpha == {}
