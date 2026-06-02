import json
import pytest
import aiosqlite
from app.db import Database, merge_trade_metadata


@pytest.fixture
async def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.init()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_init_creates_tables(db):
    async with aiosqlite.connect(db.db_path) as conn:
        cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in await cursor.fetchall()}
    assert "alphas" in tables
    assert "positions" in tables
    assert "trades" in tables
    assert "signals" in tables


@pytest.mark.asyncio
async def test_register_alpha(db):
    await db.register_alpha("test-alpha")
    alpha = await db.get_alpha("test-alpha")
    assert alpha is not None
    assert alpha["alpha_id"] == "test-alpha"
    assert alpha["status"] == "active"


@pytest.mark.asyncio
async def test_register_alpha_idempotent(db):
    await db.register_alpha("test-alpha")
    await db.register_alpha("test-alpha")
    alpha = await db.get_alpha("test-alpha")
    assert alpha is not None


@pytest.mark.asyncio
async def test_create_and_get_position(db):
    await db.register_alpha("test-alpha")
    pos_id = await db.create_position(
        position_id="pos-001",
        alpha_id="test-alpha",
        signal_id="sig-001",
        symbol="BTCUSDT",
        side="LONG",
        entry_price=95000.0,
        qty=0.01,
        tp=97000.0,
        sl=94000.0,
        leverage=10,
        opened_at="2026-05-22T10:00:00Z",
        metadata="{}",
    )
    assert pos_id == "pos-001"
    pos = await db.get_position("pos-001")
    assert pos["symbol"] == "BTCUSDT"
    assert pos["side"] == "LONG"
    assert pos["entry_price"] == 95000.0


@pytest.mark.asyncio
async def test_get_open_position_by_alpha_symbol(db):
    await db.register_alpha("test-alpha")
    await db.create_position(
        position_id="pos-001",
        alpha_id="test-alpha",
        signal_id="sig-001",
        symbol="BTCUSDT",
        side="LONG",
        entry_price=95000.0,
        qty=0.01,
        opened_at="2026-05-22T10:00:00Z",
    )
    pos = await db.get_open_position_by_alpha_symbol("test-alpha", "BTCUSDT")
    assert pos is not None
    assert pos["position_id"] == "pos-001"


@pytest.mark.asyncio
async def test_modify_position(db):
    await db.register_alpha("test-alpha")
    await db.create_position(
        position_id="pos-001",
        alpha_id="test-alpha",
        signal_id="sig-001",
        symbol="BTCUSDT",
        side="LONG",
        entry_price=95000.0,
        qty=0.01,
        sl=94000.0,
        opened_at="2026-05-22T10:00:00Z",
    )
    await db.modify_position("pos-001", sl=94500.0, tp=97000.0)
    pos = await db.get_position("pos-001")
    assert pos["sl"] == 94500.0
    assert pos["tp"] == 97000.0


@pytest.mark.asyncio
async def test_close_position(db):
    await db.register_alpha("test-alpha")
    await db.create_position(
        position_id="pos-001",
        alpha_id="test-alpha",
        signal_id="sig-001",
        symbol="BTCUSDT",
        side="LONG",
        entry_price=95000.0,
        qty=0.01,
        leverage=10,
        opened_at="2026-05-22T10:00:00Z",
    )
    await db.close_position(
        position_id="pos-001",
        exit_price=96000.0,
        reason="SIGNAL",
        closed_at="2026-05-22T11:00:00Z",
    )
    pos = await db.get_position("pos-001")
    assert pos is None
    trade = await db.get_trade("pos-001")
    assert trade is not None
    assert trade["pnl"] == pytest.approx(10.0)
    assert trade["reason"] == "SIGNAL"


@pytest.mark.asyncio
async def test_close_position_short(db):
    await db.register_alpha("test-alpha")
    await db.create_position(
        position_id="pos-002",
        alpha_id="test-alpha",
        signal_id="sig-002",
        symbol="BTCUSDT",
        side="SHORT",
        entry_price=95000.0,
        qty=0.01,
        leverage=10,
        opened_at="2026-05-22T10:00:00Z",
    )
    await db.close_position(
        position_id="pos-002",
        exit_price=94000.0,
        reason="TP_HIT",
        closed_at="2026-05-22T11:00:00Z",
    )
    trade = await db.get_trade("pos-002")
    assert trade["pnl"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_log_signal(db):
    await db.log_signal(
        signal_id="sig-001",
        alpha_id="test-alpha",
        signal_type="OPEN",
        payload='{"type":"OPEN"}',
    )
    signals = await db.get_signals(alpha_id="test-alpha", limit=10)
    assert len(signals) == 1
    assert signals[0]["signal_id"] == "sig-001"


@pytest.mark.asyncio
async def test_get_symbols_with_open_positions(db):
    await db.register_alpha("alpha-a")
    await db.register_alpha("alpha-b")
    await db.create_position(
        position_id="pos-001", alpha_id="alpha-a", signal_id="sig-001",
        symbol="BTCUSDT", side="LONG", entry_price=95000.0, qty=0.01,
        tp=97000.0, sl=94000.0, opened_at="2026-05-22T10:00:00Z",
    )
    await db.create_position(
        position_id="pos-002", alpha_id="alpha-b", signal_id="sig-002",
        symbol="ETHUSDT", side="SHORT", entry_price=3000.0, qty=0.1,
        opened_at="2026-05-22T10:00:00Z",
    )
    symbols = await db.get_symbols_with_open_positions()
    assert "BTCUSDT" in symbols
    assert "ETHUSDT" in symbols


@pytest.mark.asyncio
async def test_get_trades_by_alpha(db):
    await db.register_alpha("test-alpha")
    await db.create_position(
        position_id="pos-001",
        alpha_id="test-alpha",
        signal_id="sig-001",
        symbol="BTCUSDT",
        side="LONG",
        entry_price=95000.0,
        qty=0.01,
        leverage=10,
        opened_at="2026-05-22T10:00:00Z",
    )
    await db.close_position(
        position_id="pos-001",
        exit_price=96000.0,
        reason="SIGNAL",
        closed_at="2026-05-22T11:00:00Z",
    )
    trades = await db.get_trades_by_alpha("test-alpha")
    assert len(trades) == 1
    assert trades[0]["alpha_id"] == "test-alpha"


@pytest.mark.asyncio
async def test_register_alpha_columns(db):
    await db.register_alpha("test-alpha")
    columns = [
        {"key": "atr", "label": "ATR", "type": "number", "decimals": 6},
        {"key": "trend", "label": "Trend", "type": "text"},
    ]
    await db.register_alpha_columns("test-alpha", columns)
    result = await db.get_alpha_columns("test-alpha")
    assert len(result) == 2
    assert result[0]["column_key"] == "atr"
    assert result[0]["label"] == "ATR"
    assert result[0]["type"] == "number"
    assert result[0]["decimals"] == 6
    assert result[0]["sort_order"] == 0
    assert result[1]["column_key"] == "trend"
    assert result[1]["sort_order"] == 1


@pytest.mark.asyncio
async def test_register_alpha_columns_replaces_existing(db):
    await db.register_alpha("test-alpha")
    await db.register_alpha_columns("test-alpha", [
        {"key": "atr", "label": "ATR", "type": "number", "decimals": 6},
    ])
    await db.register_alpha_columns("test-alpha", [
        {"key": "vol_spike", "label": "Vol Spike", "type": "number", "decimals": 2},
        {"key": "btc_adx", "label": "BTC ADX", "type": "number", "decimals": 2},
    ])
    result = await db.get_alpha_columns("test-alpha")
    assert len(result) == 2
    assert result[0]["column_key"] == "vol_spike"
    assert result[1]["column_key"] == "btc_adx"


@pytest.mark.asyncio
async def test_get_alpha_columns_empty(db):
    result = await db.get_alpha_columns("nonexistent-alpha")
    assert result == []


# --- merge_trade_metadata ---

def test_merge_trade_metadata_open_only():
    result = merge_trade_metadata('{"atr": 0.001, "poc": 0.5}', None)
    parsed = json.loads(result)
    assert parsed["atr"] == 0.001
    assert parsed["poc"] == 0.5
    assert "close" not in parsed


def test_merge_trade_metadata_with_close():
    close_meta = json.dumps({"close_model": "price_alert_side_aware", "trigger_price": 0.058865})
    result = merge_trade_metadata('{"atr": 0.001}', close_meta)
    parsed = json.loads(result)
    assert parsed["atr"] == 0.001
    assert parsed["close"]["close_model"] == "price_alert_side_aware"
    assert parsed["close"]["trigger_price"] == 0.058865


def test_merge_trade_metadata_invalid_open_json():
    result = merge_trade_metadata("not-json", None)
    parsed = json.loads(result)
    assert parsed["open_raw"] == "not-json"


def test_merge_trade_metadata_invalid_close_json():
    result = merge_trade_metadata('{"atr": 1}', "bad-json")
    parsed = json.loads(result)
    assert parsed["atr"] == 1
    assert parsed["close_raw"] == "bad-json"


def test_merge_trade_metadata_both_none():
    result = merge_trade_metadata(None, None)
    assert result == "{}"


@pytest.mark.asyncio
async def test_close_position_preserves_open_metadata(db):
    await db.register_alpha("test-alpha")
    await db.create_position(
        position_id="pos-meta-01",
        alpha_id="test-alpha",
        signal_id="sig-001",
        symbol="BTCUSDT",
        side="LONG",
        entry_price=95000.0,
        qty=0.01,
        leverage=10,
        opened_at="2026-05-22T10:00:00Z",
        metadata='{"atr": 0.001, "poc": 0.5}',
    )
    close_meta = json.dumps({"close_model": "price_alert_side_aware", "trigger_price": 94000.0})
    await db.close_position(
        position_id="pos-meta-01",
        exit_price=94000.0,
        reason="SL",
        closed_at="2026-05-22T11:00:00Z",
        close_metadata=close_meta,
    )
    trade = await db.get_trade("pos-meta-01")
    assert trade is not None
    meta = json.loads(trade["metadata"])
    assert meta["atr"] == 0.001
    assert meta["poc"] == 0.5
    assert meta["close"]["close_model"] == "price_alert_side_aware"
    assert meta["close"]["trigger_price"] == 94000.0


@pytest.mark.asyncio
async def test_close_position_no_close_metadata(db):
    await db.register_alpha("test-alpha")
    await db.create_position(
        position_id="pos-nometa",
        alpha_id="test-alpha",
        signal_id="sig-002",
        symbol="ETHUSDT",
        side="SHORT",
        entry_price=3000.0,
        qty=0.1,
        leverage=5,
        opened_at="2026-05-22T10:00:00Z",
        metadata='{"atr": 0.002}',
    )
    await db.close_position(
        position_id="pos-nometa",
        exit_price=2900.0,
        reason="TP",
        closed_at="2026-05-22T12:00:00Z",
    )
    trade = await db.get_trade("pos-nometa")
    meta = json.loads(trade["metadata"])
    assert meta["atr"] == 0.002
    assert "close" not in meta
