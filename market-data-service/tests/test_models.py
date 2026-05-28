from app.models import KlineCandle, TickerUpdate


def test_kline_candle_creation():
    candle = KlineCandle(
        symbol="BTCUSDT",
        tf="1m",
        open=67000.0,
        high=67500.0,
        low=66800.0,
        close=67200.0,
        volume=12345.6,
        open_time=1716768000000,
        close_time=1716771599999,
        confirmed=True,
        correction=False,
    )
    assert candle.symbol == "BTCUSDT"
    assert candle.tf == "1m"
    assert candle.confirmed is True
    assert candle.correction is False


def test_kline_candle_to_dict():
    candle = KlineCandle(
        symbol="BTCUSDT",
        tf="1h",
        open=67000.0,
        high=67500.0,
        low=66800.0,
        close=67200.0,
        volume=12345.6,
        open_time=1716768000000,
        close_time=1716771599999,
    )
    data = candle.to_dict()
    assert data["symbol"] == "BTCUSDT"
    assert data["tf"] == "1h"
    assert data["confirmed"] is True
    assert data["correction"] is False
    assert data["open"] == 67000.0
    assert data["volume"] == 12345.6


def test_kline_candle_from_ws_1m():
    payload = {
        "e": "kline",
        "s": "ETHUSDT",
        "k": {
            "t": 1716768000000,
            "T": 1716771599999,
            "o": "3000.0",
            "h": "3050.0",
            "l": "2990.0",
            "c": "3020.0",
            "v": "500.0",
            "x": True,
        },
    }
    candle = KlineCandle.from_ws_1m(payload)
    assert candle is not None
    assert candle.symbol == "ETHUSDT"
    assert candle.tf == "1m"
    assert candle.open == 3000.0
    assert candle.high == 3050.0
    assert candle.low == 2990.0
    assert candle.close == 3020.0
    assert candle.volume == 500.0


def test_kline_candle_from_wrapped_ws_1m():
    payload = {
        "stream": "ethusdt@kline_1m",
        "data": {
            "e": "kline",
            "s": "ETHUSDT",
            "k": {
                "t": 1716768000000,
                "T": 1716771599999,
                "o": "3000.0",
                "h": "3050.0",
                "l": "2990.0",
                "c": "3020.0",
                "v": "500.0",
                "x": True,
            },
        },
    }
    candle = KlineCandle.from_ws_1m(payload)
    assert candle is not None
    assert candle.symbol == "ETHUSDT"


def test_kline_candle_from_ws_1m_partial_ignored():
    payload = {
        "e": "kline",
        "s": "ETHUSDT",
        "k": {
            "t": 1716768000000,
            "T": 1716771599999,
            "o": "3000.0",
            "h": "3050.0",
            "l": "2990.0",
            "c": "3020.0",
            "v": "500.0",
            "x": False,
        },
    }
    assert KlineCandle.from_ws_1m(payload) is None


def test_ticker_update_from_binance_ws():
    ticker = TickerUpdate.from_binance_ws(
        {
            "e": "24hrTicker",
            "s": "BTCUSDT",
            "c": "67200.50",
            "E": 1716771600000,
        }
    )
    assert ticker.symbol == "BTCUSDT"
    assert ticker.price == 67200.50
    assert ticker.timestamp == 1716771600000
    assert ticker.exchange == "binance"
