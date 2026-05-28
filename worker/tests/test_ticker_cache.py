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
