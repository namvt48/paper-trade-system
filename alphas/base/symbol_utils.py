from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)


def get_binance_perp_symbols() -> list[str]:
    try:
        response = requests.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=15)
        response.raise_for_status()
        data = response.json()
        symbols = [
            item["symbol"]
            for item in data.get("symbols", [])
            if item.get("quoteAsset") == "USDT"
            and item.get("contractType") == "PERPETUAL"
            and item.get("status") == "TRADING"
        ]
        return sorted(symbols)
    except Exception as exc:
        logger.warning("Failed to fetch Binance perp symbols: %s", exc)
        return ["BTCUSDT", "ETHUSDT"]


def get_top_n_binance_perps(n: int) -> list[str]:
    symbols = get_binance_perp_symbols()
    return symbols[:n]
