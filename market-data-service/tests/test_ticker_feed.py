from app.ticker_feed import TickerFeed


def test_build_ticker_streams():
    feed = TickerFeed()
    assert feed.build_ticker_streams(["BTCUSDT", "ETHUSDT"]) == ["btcusdt@ticker", "ethusdt@ticker"]


def test_batch_symbols():
    feed = TickerFeed()
    symbols = [f"SYM{i}USDT" for i in range(350)]
    batches = feed.batch_symbols(symbols)
    assert len(batches) == 3
    assert len(batches[0]) == 150


def test_parse_binance_ticker():
    feed = TickerFeed()
    ticker = feed.parse_binance_ticker(
        {
            "e": "24hrTicker",
            "E": 1716771600000,
            "s": "BTCUSDT",
            "c": "67200.50",
        }
    )
    assert ticker is not None
    assert ticker.symbol == "BTCUSDT"
    assert ticker.price == 67200.50
    assert ticker.exchange == "binance"


def test_parse_binance_ticker_wrapped():
    feed = TickerFeed()
    ticker = feed.parse_binance_ticker(
        {
            "stream": "btcusdt@ticker",
            "data": {
                "e": "24hrTicker",
                "E": 1716771600000,
                "s": "BTCUSDT",
                "c": "67200.50",
            },
        }
    )
    assert ticker is not None
    assert ticker.symbol == "BTCUSDT"


def test_parse_binance_ticker_non_ticker():
    assert TickerFeed().parse_binance_ticker({"e": "kline", "s": "BTCUSDT"}) is None
