import json
import sqlite3
from datetime import datetime

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from scripts.backfill_shadow_equity import MdsPriceLookup, repair_shadow_equity


class _FakePriceLookup:
    def __init__(self, prices: dict[tuple[str, str], float]) -> None:
        self._prices = prices

    def get_price(self, symbol: str, timestamp: datetime) -> float | None:
        return self._prices.get((symbol, timestamp.isoformat()))


def _create_trade_db(path: str) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE virtual_trade_events (
            event_id TEXT PRIMARY KEY,
            alpha_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            processed_at TEXT NOT NULL
        )
        """
    )
    opened = {
        "event_id": "open-btc",
        "type": "VIRTUAL_OPEN",
        "alpha_id": "1h-test-sleeve",
        "position_id": "btc-long",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "price": 100.0,
        "qty": 1.0,
        "timestamp": "2026-07-25T00:00:00+00:00",
        "timeframe": "1h",
    }
    con.execute(
        """
        INSERT INTO virtual_trade_events
            (event_id, alpha_id, event_type, payload, processed_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            opened["event_id"],
            opened["alpha_id"],
            opened["type"],
            json.dumps(opened),
            opened["timestamp"],
        ),
    )
    con.commit()
    con.close()


def _create_snapshot_db(path: str) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE equity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            alpha_id TEXT NOT NULL,
            balance REAL NOT NULL,
            unrealized_pnl REAL DEFAULT 0,
            realized_pnl REAL DEFAULT 0
        )
        """
    )
    con.executemany(
        """
        INSERT INTO equity_snapshots
            (timestamp, alpha_id, balance, unrealized_pnl, realized_pnl)
        VALUES (?, '1h-test-sleeve', ?, ?, 0)
        """,
        [
            ("2026-07-25T00:04:00+00:00", 10_000.0, 0.0),
            ("2026-07-25T00:19:00+00:00", 10_000.0, 0.0),
            ("2026-07-25T00:34:00+00:00", 10_000.0, 0.0),
            ("2026-07-25T00:49:00+00:00", 10_000.0, 0.0),
            # New 1h strategy bucket: preserve this canonical runner anchor.
            ("2026-07-25T01:04:00+00:00", 10_020.0, 20.0),
        ],
    )
    con.commit()
    con.close()


def test_repair_marks_flat_shadow_rows_between_canonical_runner_anchors(tmp_path):
    trade_db = tmp_path / "paper-trade.db"
    snapshot_db = tmp_path / "equity-snapshots.db"
    _create_trade_db(str(trade_db))
    _create_snapshot_db(str(snapshot_db))

    lookup = _FakePriceLookup(
        {
            ("BTCUSDT", "2026-07-25T00:04:00+00:00"): 100.0,
            ("BTCUSDT", "2026-07-25T00:19:00+00:00"): 105.0,
            ("BTCUSDT", "2026-07-25T00:34:00+00:00"): 110.0,
            ("BTCUSDT", "2026-07-25T00:49:00+00:00"): 115.0,
            ("BTCUSDT", "2026-07-25T01:04:00+00:00"): 120.0,
        }
    )

    result = repair_shadow_equity(
        trade_db_path=str(trade_db),
        snapshot_db_path=str(snapshot_db),
        price_lookup=lookup,
        before="2026-07-25T02:00:00+00:00",
    )

    con = sqlite3.connect(snapshot_db)
    rows = con.execute(
        """
        SELECT balance, unrealized_pnl, realized_pnl
        FROM equity_snapshots
        ORDER BY timestamp
        """
    ).fetchall()
    con.close()

    assert result.changed_rows == 3
    assert [row[0] for row in rows] == pytest.approx(
        [10_000.0, 10_005.0, 10_010.0, 10_015.0, 10_020.0]
    )
    assert [row[1] for row in rows] == pytest.approx([0.0, 5.0, 10.0, 15.0, 20.0])
    assert [row[2] for row in rows] == pytest.approx([0.0, 0.0, 0.0, 0.0, 0.0])


def test_mds_lookup_never_reads_an_incomplete_candle(tmp_path):
    symbol_dir = tmp_path / "15m" / "BTCUSDT"
    symbol_dir.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "open_time": [
                    int(parse.timestamp() * 1000)
                    for parse in (
                        datetime.fromisoformat("2026-07-25T00:00:00+00:00"),
                        datetime.fromisoformat("2026-07-25T00:15:00+00:00"),
                    )
                ],
                "close": [100.0, 110.0],
            }
        ),
        symbol_dir / "base.parquet",
    )
    lookup = MdsPriceLookup(str(tmp_path), "15m")

    assert (
        lookup.get_price(
            "BTCUSDT",
            datetime.fromisoformat("2026-07-25T00:14:59+00:00"),
        )
        is None
    )
    assert lookup.get_price(
        "BTCUSDT",
        datetime.fromisoformat("2026-07-25T00:15:00+00:00"),
    ) == pytest.approx(100.0)
    assert lookup.get_price(
        "BTCUSDT",
        datetime.fromisoformat("2026-07-25T00:29:59+00:00"),
    ) == pytest.approx(100.0)
    assert lookup.get_price(
        "BTCUSDT",
        datetime.fromisoformat("2026-07-25T00:30:00+00:00"),
    ) == pytest.approx(110.0)
