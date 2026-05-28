from __future__ import annotations

from collections import defaultdict

from app.models import KlineCandle


TF_MINUTES = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}


class Aggregator:
    def __init__(
        self,
        timeframes: list[str] | None = None,
        max_1m_per_symbol: int = 15000,
        max_tf_per_symbol: int = 1500,
    ):
        self.timeframes = timeframes or ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
        self._1m_candles: dict[str, list[KlineCandle]] = defaultdict(list)
        self._tf_candles: dict[str, dict[str, list[KlineCandle]]] = defaultdict(lambda: defaultdict(list))
        self._max_1m_per_symbol = max_1m_per_symbol
        self._max_tf_per_symbol = max_tf_per_symbol

    def on_1m_close(self, candle: KlineCandle) -> list[KlineCandle]:
        symbol = candle.symbol
        self._append_or_replace(self._1m_candles[symbol], candle)
        self._trim(self._1m_candles[symbol], self._max_1m_per_symbol)

        results = [candle]
        for tf in self.timeframes:
            if tf == "1m":
                continue

            tf_minutes = TF_MINUTES.get(tf)
            if tf_minutes is None:
                continue

            tf_ms = tf_minutes * 60 * 1000
            candle_close_exclusive = candle.open_time + 60 * 1000
            tf_boundary = ((candle.open_time // tf_ms) + 1) * tf_ms
            if candle_close_exclusive < tf_boundary:
                continue

            tf_open_time = tf_boundary - tf_ms
            parts = [
                c
                for c in self._1m_candles[symbol]
                if tf_open_time <= c.open_time < tf_boundary
            ]
            if len(parts) < tf_minutes:
                continue

            rolled = self._rollup(parts, symbol, tf, tf_open_time, tf_boundary - 1)
            self._append_or_replace(self._tf_candles[symbol][tf], rolled)
            self._trim(self._tf_candles[symbol][tf], self._max_tf_per_symbol)
            results.append(rolled)

        return results

    def apply_correction(self, correction: KlineCandle) -> None:
        if correction.tf == "1m":
            self._append_or_replace(self._1m_candles[correction.symbol], correction)
            return
        self._append_or_replace(self._tf_candles[correction.symbol][correction.tf], correction)

    def get_candles(self, symbol: str, tf: str) -> list[KlineCandle]:
        if tf == "1m":
            return list(self._1m_candles.get(symbol, []))
        return list(self._tf_candles.get(symbol, {}).get(tf, []))

    def _rollup(
        self,
        candles: list[KlineCandle],
        symbol: str,
        tf: str,
        open_time: int,
        close_time: int,
    ) -> KlineCandle:
        candles = sorted(candles, key=lambda c: c.open_time)
        return KlineCandle(
            symbol=symbol,
            tf=tf,
            open=candles[0].open,
            high=max(c.high for c in candles),
            low=min(c.low for c in candles),
            close=candles[-1].close,
            volume=sum(c.volume for c in candles),
            open_time=open_time,
            close_time=close_time,
            confirmed=True,
            correction=False,
        )

    @staticmethod
    def _append_or_replace(store: list[KlineCandle], candle: KlineCandle) -> None:
        for index, existing in enumerate(store):
            if existing.open_time == candle.open_time:
                store[index] = candle
                return
        store.append(candle)
        store.sort(key=lambda c: c.open_time)

    @staticmethod
    def _trim(store: list[KlineCandle], max_len: int) -> None:
        if len(store) > max_len:
            del store[: len(store) - max_len]
