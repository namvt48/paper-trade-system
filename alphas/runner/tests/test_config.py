from __future__ import annotations

from runner.config import load_runner_config, WarmupConfig


def test_config_loads_yaml_and_ignores_disabled(tmp_path):
    path = tmp_path / "runner.yaml"
    path.write_text(
        """
runner_id: yaml-runner
redis_url: redis://paper
mds_redis_url: redis://mds
shadow_mode: true
warmup:
  max_concurrent_mds_requests: 2
  max_mds_requests_per_minute: 10
  max_symbols_per_mds_request: 7
  request_timeout_sec: 12
  response_cache_ttl_sec: 123
alphas:
  - alpha_id: a1
    strategy: test_strategy
    enabled: true
    params: {tf: 15m}
  - alpha_id: a2
    strategy: test_strategy
    enabled: false
""",
        encoding="utf-8",
    )

    cfg = load_runner_config(path)

    assert cfg.runner_id == "yaml-runner"
    assert cfg.redis_url == "redis://paper"
    assert cfg.mds_redis_url == "redis://mds"
    assert cfg.warmup.max_concurrent_mds_requests == 2
    assert cfg.warmup.max_mds_requests_per_minute == 10
    assert cfg.warmup.max_symbols_per_mds_request == 7
    assert cfg.warmup.request_timeout_sec == 12
    assert cfg.warmup.response_cache_ttl_sec == 123
    assert [a.alpha_id for a in cfg.alphas] == ["a1"]


def test_config_runner_id_env_overrides_yaml(tmp_path, monkeypatch):
    path = tmp_path / "runner.yaml"
    path.write_text("runner_id: yaml-runner\nalphas: []\n", encoding="utf-8")
    monkeypatch.setenv("RUNNER_ID", "env-runner")

    assert load_runner_config(path).runner_id == "env-runner"


def test_config_default_signal_stream_respects_shadow_mode(tmp_path):
    shadow = tmp_path / "shadow.yaml"
    shadow.write_text("shadow_mode: true\nalphas: []\n", encoding="utf-8")
    prod = tmp_path / "prod.yaml"
    prod.write_text("shadow_mode: false\nalphas: []\n", encoding="utf-8")

    assert load_runner_config(shadow).signal_stream == "paper-signals-shadow"
    assert load_runner_config(prod).signal_stream == "paper-signals"


def test_config_supports_nested_runner_section_and_rejects_shadow_prod_stream(tmp_path):
    ok = tmp_path / "nested.yaml"
    ok.write_text(
        """
runner:
  runner_id: nested
  shadow_mode: true
  signal_stream: paper-signals-shadow
alphas: []
""",
        encoding="utf-8",
    )
    assert load_runner_config(ok).runner_id == "nested"

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
runner:
  shadow_mode: true
  signal_stream: paper-signals
alphas: []
""",
        encoding="utf-8",
    )
    try:
        load_runner_config(bad)
    except ValueError as exc:
        assert "shadow_mode=true" in str(exc)
    else:
        raise AssertionError("invalid shadow/prod stream config should fail")


def test_warmup_config_new_defaults():
    cfg = WarmupConfig()
    assert cfg.mds_ready_timeout_sec == 900
    assert cfg.min_warmup_coverage_pct == 0.60
    assert cfg.sync_tolerance_candles == 1
    assert cfg.reconnect_staleness_candles == 5


def test_warmup_config_new_fields_from_yaml():
    raw = {
        "mds_ready_timeout_sec": 600,
        "min_warmup_coverage_pct": 0.80,
        "sync_tolerance_candles": 2,
        "reconnect_staleness_candles": 10,
    }
    cfg = WarmupConfig(
        max_concurrent_mds_requests=3,
        max_mds_requests_per_minute=20,
        max_symbols_per_mds_request=10,
        request_timeout_sec=60.0,
        response_cache_ttl_sec=300.0,
        mds_ready_timeout_sec=int(raw.get("mds_ready_timeout_sec", 900)),
        min_warmup_coverage_pct=float(raw.get("min_warmup_coverage_pct", 0.60)),
        sync_tolerance_candles=int(raw.get("sync_tolerance_candles", 1)),
        reconnect_staleness_candles=int(raw.get("reconnect_staleness_candles", 5)),
    )
    assert cfg.mds_ready_timeout_sec == 600
    assert cfg.min_warmup_coverage_pct == 0.80
    assert cfg.sync_tolerance_candles == 2
    assert cfg.reconnect_staleness_candles == 10


def test_load_config_with_multi_runner_fields(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("""
runner:
  runner_id: test
  max_alphas_per_runner: 8
  claim_interval_sec: 15
  runner_cache_dir: /data/cache
  claim_retry_delay_sec: 5
modules: []
alphas: []
""")
    cfg = load_runner_config(str(p))
    assert cfg.max_alphas_per_runner == 8
    assert cfg.claim_interval_sec == 15
    assert cfg.runner_cache_dir == "/data/cache"
    assert cfg.claim_retry_delay_sec == 5
