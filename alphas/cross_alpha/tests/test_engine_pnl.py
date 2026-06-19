from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from cross_alpha.engine import CrossSectionalEngine


def _make_engine() -> CrossSectionalEngine:
    with patch.object(CrossSectionalEngine, "__init__", lambda self, *a, **kw: None):
        eng = CrossSectionalEngine.__new__(CrossSectionalEngine)
    eng.alpha_id = "15m-blend-close"
    eng._open_positions = {
        "BTCUSDT": {
            "position_id": "test:BTCUSDT:LONG:1000",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "entry": 65000.0,
            "qty": 0.1,
            "weight": 0.05,
        },
    }
    eng._last_pnl_publish = {}
    eng._pnl_channel = "pnl:15m-blend-close"
    return eng


@pytest.mark.asyncio
async def test_on_price_alert_message_publishes_pnl():
    eng = _make_engine()
    msg = {"symbol": "BTCUSDT", "bid": 66000.0, "ask": 66010.0, "side": "bid"}

    with patch("base.signal_push._r") as mock_redis:
        eng.on_price_alert_message(msg)
        mock_redis.publish.assert_called_once()
        call_args = mock_redis.publish.call_args
        assert call_args[0][0] == "pnl:15m-blend-close"
        payload = json.loads(call_args[0][1])
        assert payload["symbol"] == "BTCUSDT"
        assert payload["side"] == "LONG"
        assert payload["current_price"] == 66000.0
        assert abs(payload["pnl_pct"] - (66000.0 - 65000.0) / 65000.0) < 1e-6


@pytest.mark.asyncio
async def test_on_price_alert_message_skips_no_position():
    eng = _make_engine()
    msg = {"symbol": "SOLUSDT", "bid": 150.0, "ask": 150.1, "side": "bid"}

    with patch("base.signal_push._r") as mock_redis:
        eng.on_price_alert_message(msg)
        mock_redis.publish.assert_not_called()


@pytest.mark.asyncio
async def test_on_price_alert_message_throttles():
    eng = _make_engine()
    msg = {"symbol": "BTCUSDT", "bid": 66000.0, "ask": 66010.0, "side": "bid"}

    with patch("base.signal_push._r") as mock_redis:
        eng.on_price_alert_message(msg)
        assert mock_redis.publish.call_count == 1

        eng.on_price_alert_message(msg)
        assert mock_redis.publish.call_count == 1  # throttled

        eng._last_pnl_publish["BTCUSDT"] = time.time() - 1.0
        eng.on_price_alert_message(msg)
        assert mock_redis.publish.call_count == 2
