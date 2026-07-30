from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from cross_alpha.strategy import Selection

from scripts.rebalance_recovery.configuration import RecoveryAlphaConfig
from scripts.rebalance_recovery.domain import AlphaId, CandleOpenMs, RecoveryPoint
from scripts.rebalance_recovery.market import capture_market_inputs
from scripts.rebalance_recovery.replay import _tradable_balanced_weights
from scripts.rebalance_recovery.replay_state import ReplayError
from scripts.rebalance_recovery.storage import Workspace


class EmptyRedisReader:
    def lrange(self, name: str, start: int, end: int) -> tuple[str, ...]:
        return ()


def _workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "run"
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    return Workspace(
        root=root,
        inputs=inputs,
        baseline_main=root / "baseline-main.db",
        baseline_equity=root / "baseline-equity.db",
        candidate_main=root / "candidate-main.db",
        candidate_equity=root / "candidate-equity.db",
        ledger=root / "ledger.jsonl",
        manifest=root / "manifest.json",
        report_json=root / "report.json",
        report_markdown=root / "report.md",
        redis_state=root / "redis.json",
    )


def _config(tmp_path: Path, alpha_id: str, warmup_bars: int) -> RecoveryAlphaConfig:
    alpha_root = tmp_path / alpha_id
    alpha_root.mkdir()
    spec = alpha_root / "spec.json"
    whitelist = alpha_root / "whitelist.txt"
    spec.write_text("{}", encoding="utf-8")
    whitelist.write_text("BTCUSDT\n", encoding="utf-8")
    return RecoveryAlphaConfig(
        alpha_id=AlphaId(alpha_id),
        version="1",
        spec_path=spec,
        whitelist_path=whitelist,
        blacklist_path=None,
        warmup_bars=warmup_bars,
        capital=10_000.0,
        exchange="binance",
    )


def _write_candles(cache: Path) -> None:
    directory = cache / "binance" / "15m" / "BTCUSDT"
    directory.mkdir(parents=True)
    count = 30
    pq.write_table(
        pa.table(
            {
                "open_time": [index * 900_000 for index in range(count)],
                "open": [100.0] * count,
                "high": [101.0] * count,
                "low": [99.0] * count,
                "close": [100.0] * count,
                "volume": [10.0] * count,
            }
        ),
        directory / "base.parquet",
    )


def test_shared_market_snapshot_keeps_largest_warmup(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    _write_candles(cache)
    configs = (_config(tmp_path, "long", 5), _config(tmp_path, "short", 2))
    event_at = datetime(1970, 1, 1, 7, 30, tzinfo=timezone.utc)
    points = tuple(
        RecoveryPoint(config.alpha_id, "15m", CandleOpenMs(29 * 900_000), event_at)
        for config in configs
    )
    workspace = _workspace(tmp_path)

    capture_market_inputs(workspace, configs, points, cache, EmptyRedisReader())

    snapshot = workspace.inputs / "market" / "15m" / "BTCUSDT.json"
    assert snapshot.read_text(encoding="utf-8").count('"open_time"') == 21


def test_replay_matches_runner_when_filtered_book_is_empty() -> None:
    selection = Selection([], [], {}, {}, {}, {}, {})

    assert _tradable_balanced_weights(selection, {}) == {}


@contextmanager
def _pass_through() -> Iterator[None]:
    yield


def test_typed_replay_error_preserves_traceback_protocol() -> None:
    with pytest.raises(ReplayError, match="expected"):
        with _pass_through():
            raise ReplayError("expected")
