from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]

SLEEVE_SOURCES = (
    "15m-blend-close",
    "15m-blend-close-2-v3",
    "15m-blend-close-36h",
    "15m-blend-close-b",
    "15m-blend-close-b-36h",
    "15m-breakout",
    "1h-blend-close",
    "1h-blend-close-c",
    "1h-decay-close",
    "1h-decay-close-36h",
    "1h-decay-close-v3",
    "1h-decay-vwap-36h",
    "1h-trend-breakout",
    "1h-trend-skew",
    "4h-amihud",
    "4h-trend-close-v3",
    "4h-trend-z",
)

PAUSED_ALPHA_IDS = {
    "15m-blend-close",
    "15m-blend-close-2-v3",
    "15m-blend-close-36h",
    "15m-blend-close-b",
    "15m-blend-close-b-36h",
    "15m-breakout",
    "1h-decay-close",
    "1h-decay-close-36h",
    "1h-decay-close-v3",
    "1h-decay-vwap-36h",
    "1h-trend-breakout",
    "4h-trend-close-v3",
    "4h-trend-z",
    "songthanv11",
    "songthanv8",
    "1d-kertrend",
}


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_paused_alphas_are_disabled_and_all_sleeves_are_enabled() -> None:
    config = _load_yaml(ROOT / "runner-config.yaml")
    alphas = {item["alpha_id"]: item for item in config["alphas"]}

    assert all(not alphas[alpha_id]["enabled"] for alpha_id in PAUSED_ALPHA_IDS)

    expected_sleeves = {f"{alpha_id}-sleeve" for alpha_id in SLEEVE_SOURCES}
    enabled_sleeves = {
        alpha_id
        for alpha_id, item in alphas.items()
        if item["enabled"] and alpha_id.endswith("-sleeve")
    }
    assert enabled_sleeves == expected_sleeves
    assert all(alphas[alpha_id]["params"]["book_only"] for alpha_id in expected_sleeves)


def test_all_sleeves_are_registered_for_paper_db_discovery() -> None:
    env_lines = (ROOT / ".env").read_text(encoding="utf-8").splitlines()
    registered_line = next(
        line for line in env_lines if line.startswith("REGISTERED_ALPHAS=")
    )
    registered_alphas = set(registered_line.partition("=")[2].split(","))

    expected_sleeves = {f"{alpha_id}-sleeve" for alpha_id in SLEEVE_SOURCES}
    assert expected_sleeves <= registered_alphas


def test_sleeve_specs_only_change_identity_and_book_only_mode() -> None:
    for source_id in SLEEVE_SOURCES:
        sleeve_id = f"{source_id}-sleeve"
        source_dir = ROOT / "alphas" / source_id
        sleeve_dir = ROOT / "alphas" / sleeve_id

        source_spec = json.loads((source_dir / "spec.json").read_text(encoding="utf-8"))
        sleeve_spec = json.loads((sleeve_dir / "spec.json").read_text(encoding="utf-8"))

        assert sleeve_spec.pop("alpha_id") == sleeve_id
        assert sleeve_spec.pop("book_only") is True
        assert sleeve_spec == {
            key: value
            for key, value in source_spec.items()
            if key not in {"alpha_id", "book_only"}
        }
        assert (sleeve_dir / "whitelist.txt").read_bytes() == (
            source_dir / "whitelist.txt"
        ).read_bytes()
        assert (sleeve_dir / "blacklist.txt").read_bytes() == (
            source_dir / "blacklist.txt"
        ).read_bytes()


def test_portfolio_uses_all_sleeves_with_normalized_weights() -> None:
    config = json.loads(
        (ROOT / "portfolio_manager" / "config" / "portfolio.json").read_text(
            encoding="utf-8"
        )
    )
    sleeves = config["sleeves"]

    assert {item["id"] for item in sleeves} == {
        f"{alpha_id}-sleeve" for alpha_id in SLEEVE_SOURCES
    }
    assert len(sleeves) == len(SLEEVE_SOURCES)
    assert abs(sum(float(item["weight"]) for item in sleeves) - 1.0) < 1e-9
