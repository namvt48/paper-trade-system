"""Parse runner configuration and pin every alpha input used by recovery."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from .domain import AlphaId
from .scope import INCIDENT_SCHEDULES


@dataclass(frozen=True, slots=True)
class RecoveryAlphaConfig:
    """Resolved runner parameters required to reproduce one alpha selection."""

    alpha_id: AlphaId
    version: str
    spec_path: Path
    whitelist_path: Path
    blacklist_path: Path | None
    warmup_bars: int
    capital: float
    exchange: str


class ConfigurationError(Exception):
    """Runner configuration cannot satisfy the frozen incident scope."""


def load_recovery_configs(
    runner_config: Path,
    alphas_dir: Path,
) -> tuple[RecoveryAlphaConfig, ...]:
    """Parse and resolve every scoped alpha from a runner YAML file."""
    raw = yaml.safe_load(runner_config.read_text(encoding="utf-8")) or {}
    configured = {str(item["alpha_id"]): item for item in raw.get("alphas", [])}
    configs: list[RecoveryAlphaConfig] = []
    for schedule in INCIDENT_SCHEDULES:
        alpha_id = str(schedule.alpha_id)
        item = configured.get(alpha_id)
        if item is None:
            raise ConfigurationError(f"alpha missing from runner config: {alpha_id}")
        params = item.get("params") or {}
        spec_path = _resolve_alpha_path(
            alphas_dir, params.get("spec_file"), alpha_id, "spec.json"
        )
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        if spec.get("timeframe") != schedule.timeframe:
            raise ConfigurationError(f"timeframe drift for {alpha_id}")
        if int(spec.get("rebalance_bars", 0)) != schedule.rebalance_bars:
            raise ConfigurationError(f"rebalance cadence drift for {alpha_id}")
        whitelist_path = _resolve_alpha_path(
            alphas_dir,
            params.get("whitelist_file"),
            alpha_id,
            "whitelist.txt",
        )
        blacklist_path = _optional_alpha_path(alphas_dir, params.get("blacklist_file"))
        configs.append(
            RecoveryAlphaConfig(
                alpha_id=schedule.alpha_id,
                version=str(item.get("version", "1")),
                spec_path=spec_path,
                whitelist_path=whitelist_path,
                blacklist_path=blacklist_path,
                warmup_bars=int(params.get("warmup_bars", 0)),
                capital=float(params.get("capital", 10_000.0)),
                exchange=str(params.get("exchange", "binance")),
            )
        )
    return tuple(configs)


def load_symbols(config: RecoveryAlphaConfig) -> tuple[str, ...]:
    """Load the same whitelist-minus-blacklist universe used by the runner."""
    blacklist = (
        _read_symbol_file(config.blacklist_path) if config.blacklist_path else set()
    )
    symbols = _read_symbol_file(config.whitelist_path)
    return tuple(sorted(symbol for symbol in symbols if symbol not in blacklist))


def _read_symbol_file(path: Path) -> set[str]:
    """Parse a newline-delimited symbol file into normalized identifiers."""
    return {
        line.strip().upper()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _resolve_alpha_path(
    alphas_dir: Path,
    configured: str | None,
    alpha_id: str,
    fallback_name: str,
) -> Path:
    """Resolve a required config path and fail before capture if it is absent."""
    path = alphas_dir / (configured or f"{alpha_id}/{fallback_name}")
    if not path.exists():
        raise ConfigurationError(f"required alpha input missing: {path}")
    return path


def _optional_alpha_path(alphas_dir: Path, configured: str | None) -> Path | None:
    """Resolve an optional blacklist without inventing an empty source file."""
    if not configured:
        return None
    path = alphas_dir / configured
    return path if path.exists() else None
