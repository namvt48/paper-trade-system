from unittest.mock import patch

from base.symbol_utils import get_binance_perp_symbols, get_top_n_binance_perps


@patch("base.symbol_utils.requests.get")
def test_get_binance_perp_symbols(mock_get):
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = {
        "symbols": [
            {"symbol": "BTCUSDT", "quoteAsset": "USDT", "contractType": "PERPETUAL", "status": "TRADING"},
            {"symbol": "ETHUSDT", "quoteAsset": "USDT", "contractType": "PERPETUAL", "status": "TRADING"},
            {"symbol": "BTCBUSD", "quoteAsset": "BUSD", "contractType": "PERPETUAL", "status": "TRADING"},
            {"symbol": "EXPIRED", "quoteAsset": "USDT", "contractType": "PERPETUAL", "status": "DELISTED"},
        ]
    }
    result = get_binance_perp_symbols()
    assert result == ["BTCUSDT", "ETHUSDT"]


@patch("base.symbol_utils.requests.get")
def test_get_binance_perp_symbols_fallback(mock_get):
    mock_get.side_effect = Exception("network error")
    result = get_binance_perp_symbols()
    assert result == ["BTCUSDT", "ETHUSDT"]


@patch("base.symbol_utils.get_binance_perp_symbols")
def test_get_top_n_binance_perps(mock_get_symbols):
    mock_get_symbols.return_value = ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"]
    result = get_top_n_binance_perps(2)
    assert result == ["AAAUSDT", "BBBUSDT"]


@patch("base.symbol_utils.get_binance_perp_symbols")
def test_get_top_n_binance_perps_all(mock_get_symbols):
    mock_get_symbols.return_value = ["AAAUSDT", "BBBUSDT"]
    result = get_top_n_binance_perps(10)
    assert result == ["AAAUSDT", "BBBUSDT"]
