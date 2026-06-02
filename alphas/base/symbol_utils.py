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
        return []


def get_top_n_binance_perps(n: int) -> list[str]:
    symbols = get_binance_perp_symbols()
    return symbols[:n]


def get_binance_perp_symbols_by_volume_rank(start: int, end: int) -> list[str]:
    """Return USDT perpetual symbols ranked by 24h quote volume.

    The rank slice uses zero-based indexes and is end-exclusive, matching normal
    Python slicing and the alpha q1/q2/q3 config names.
    """
    try:
        tradable = set(get_binance_perp_symbols())
        response = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=15)
        response.raise_for_status()
        rows = response.json()
        ranked = sorted(
            (
                (item["symbol"], float(item.get("quoteVolume", 0.0) or 0.0))
                for item in rows
                if item.get("symbol") in tradable
            ),
            key=lambda row: row[1],
            reverse=True,
        )
        symbols = [symbol for symbol, _volume in ranked]
        if symbols:
            return symbols[start:end]
    except Exception as exc:
        logger.warning("Failed to fetch Binance perp volume ranks: %s", exc)

    return []
