"""Universe loading for the cross-sectional runner strategy.

Whitelist-only logic: whitelist.txt IS the tradable universe. There is no
universe_file fallback — a missing whitelist is a fatal config error.
"""
import pytest

from runner.strategies.cross_sectional.strategy import CrossSectionalRunnerStrategy


def _bare(params, alphas_root):
    s = CrossSectionalRunnerStrategy.__new__(CrossSectionalRunnerStrategy)
    s.alpha_id = "test-alpha"
    s.params = params
    s._alphas_root = alphas_root
    return s


def test_whitelist_file_used_as_universe(tmp_path):
    wl = tmp_path / "whitelist.txt"
    wl.write_text("BTCUSDT\nETHUSDT\n# a comment\nsolusdt\n\n")
    s = _bare({"whitelist_file": str(wl)}, tmp_path)
    assert s._load_universe() == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_universe_file_is_ignored(tmp_path):
    # universe_file is fully removed; only the whitelist drives the universe.
    wl = tmp_path / "whitelist.txt"
    wl.write_text("BTCUSDT\n")
    uni = tmp_path / "u.json"
    uni.write_text('{"symbols": ["ETHUSDT", "XRPUSDT"]}')
    s = _bare({"whitelist_file": str(wl), "universe_file": str(uni)}, tmp_path)
    assert s._load_universe() == ["BTCUSDT"]


def test_whitelist_next_to_spec_by_convention(tmp_path):
    (tmp_path / "spec.json").write_text("{}")
    (tmp_path / "whitelist.txt").write_text("BTCUSDT\nETHUSDT\n")
    s = _bare({"spec_file": str(tmp_path / "spec.json")}, tmp_path)
    assert s._load_universe() == ["BTCUSDT", "ETHUSDT"]


def test_missing_whitelist_raises(tmp_path):
    # No whitelist file anywhere -> fatal config error (no universe_file fallback).
    (tmp_path / "spec.json").write_text("{}")
    s = _bare({"spec_file": str(tmp_path / "spec.json")}, tmp_path)
    with pytest.raises(ValueError, match="no whitelist"):
        s._load_universe()


def test_blacklist_applied_to_whitelist(tmp_path):
    wl = tmp_path / "whitelist.txt"
    wl.write_text("BTCUSDT\nETHUSDT\n")
    bl = tmp_path / "bl.txt"
    bl.write_text("ETHUSDT\n")
    s = _bare({"whitelist_file": str(wl), "blacklist_file": str(bl)}, tmp_path)
    assert s._load_universe() == ["BTCUSDT"]
