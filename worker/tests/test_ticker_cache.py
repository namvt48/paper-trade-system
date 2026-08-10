from app.main import TickerPriceCache


def test_ticker_price_cache_empty():
    assert TickerPriceCache().get_prices() == {}


def test_ticker_price_cache_update_price():
    cache = TickerPriceCache()
    cache.update_price("BTCUSDT", 95000.0)
    assert cache.get_prices() == {"BTCUSDT": 95000.0}


def test_ticker_price_cache_filters_symbols():
    cache = TickerPriceCache()
    cache.update_price("BTCUSDT", 95000.0)
    cache.update_price("ETHUSDT", 3000.0)
    assert cache.get_prices(["ETHUSDT", "SOLUSDT"]) == {"ETHUSDT": 3000.0}


def test_ticker_price_cache_rejects_stale_and_non_positive_prices():
    now = [10.0]
    cache = TickerPriceCache(staleness_sec=2.0, clock=lambda: now[0])
    cache.update_price("BTCUSDT", 95000.0)
    cache.update_price("ZERO", 0.0)
    assert cache.get_price("BTCUSDT") == 95000.0
    assert cache.get_price("ZERO") is None

    now[0] += 3.0
    assert cache.get_price("BTCUSDT") is None
    assert cache.get_last_price("BTCUSDT") == 95000.0


def test_ticker_quote_is_not_executable():
    cache = TickerPriceCache()
    cache.update_price("BTCUSDT", 95000.0)
    quote = cache.get_quote("BTCUSDT")
    assert quote.source == "ticker_mid"
    assert quote.is_executable is False
