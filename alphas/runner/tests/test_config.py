from __future__ import annotations

from runner.config import load_runner_config


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
