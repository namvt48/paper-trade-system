import asyncio
from bisect import bisect_left
import logging
import os
import signal as sig
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from math import ceil

import redis as redis_lib

from base import signal_push
from base.config import BaseConfig
from base.json_utils import dumps as json_dumps, loads as json_loads
from base.models import SymbolData
from base.position_reconcile import normalize_position, parse_snapshot, snapshot_age_sec


class BaseEngine(ABC):
    def __init__(self, config: BaseConfig):
        self.config = config
        self.symbol_data: dict[str, dict[str, SymbolData]] = {}
        self.data_lock = asyncio.Lock()
        self.shutdown_event = asyncio.Event()
        self._logger = logging.getLogger(config.ALPHA_ID)
        self._blacklist: set[str] = {
            s.strip().upper() for s in config.SYMBOL_BLACKLIST.split(",") if s.strip()
        }
        self._columns_config_path: str | None = None
        self.runtime_state = "STARTING"
        self.last_price_alert_at: dict[str, dict[str, float]] = {}
        self.last_kline_at: dict[str, dict[str, dict[str, float]]] = {}
        self.last_warmup_ok_at: dict[str, dict[str, dict[str, float]]] = {}
        self.last_redis_message_at: float = 0.0
        self.latest_price_alert: dict[str, dict] = {}
        self._positions_changed = asyncio.Event()
        self._subscribed_price_alert_symbols: set[str] = set()
        self._symbol_universe_cache: list[str] = []
        self._authoritative_revision: int | None = None
        self._authoritative_position_ids: set[str] = set()
        self._snapshot_age_sec = float("inf")
        self._heartbeat_ok = False
        self._last_reconcile_at: float = 0.0
        self._data_stale = False
        self._position_reconcile_stale = False
        self._price_alert_sync_stale = False
        self._transport_reconnect_requested = False

    @abstractmethod
    def get_required_channels(self) -> list[str]:
        """Return Redis Pub/Sub channels needed by this alpha."""

    @abstractmethod
    async def scan_loop(self) -> None:
        """Main signal scanning loop; call push_signal() when signals are found."""

    @abstractmethod
    def _get_warmup_symbols(self) -> list[str]:
        """Return symbols to request warmup data for (already filtered for blacklist)."""

    @abstractmethod
    async def _manage_positions(self) -> None:
        """Manage open positions using 1m kline data. Called by manage_loop."""

    @abstractmethod
    def _has_open_positions(self) -> bool:
        """Return True if there are open positions to manage."""

    async def on_warmup_complete(self) -> None:
        """Called after warmup data is loaded. Override to reconstruct in-memory state."""

    def restore_position(self, snapshot_position: dict) -> dict | None:
        """Restore a worker-owned position conservatively.

        Strategies may override this to reconstruct richer runtime state. The default
        preserves authoritative fields and any namespaced strategy_runtime metadata.
        """
        return normalize_position(snapshot_position)

    def serialize_position_runtime(self, position: dict) -> dict:
        worker_owned = {
            "position_id", "alpha_id", "symbol", "side", "entry", "entry_price",
            "qty", "tp", "sl", "leverage", "opened_at", "exchange", "metadata",
        }
        return {
            key: value for key, value in position.items()
            if key not in worker_owned and isinstance(value, (str, int, float, bool, type(None)))
        }

    def on_position_reconciled(self, position: dict, mode: str) -> None:
        pass

    def _local_positions_by_id(self) -> dict[str, tuple[str, dict]]:
        positions = getattr(self, "_open_positions", {})
        if not isinstance(positions, dict):
            return {}
        return {
            str(pos.get("position_id")): (str(symbol), pos)
            for symbol, pos in positions.items()
            if isinstance(pos, dict) and pos.get("position_id")
        }

    async def _accept_empty_position_reconcile(
        self,
        client,
        revision: int = 0,
        snapshot_age_sec: float = 0.0,
    ) -> bool:
        self._snapshot_age_sec = snapshot_age_sec
        self._authoritative_revision = revision
        self._authoritative_position_ids = set()
        self._last_reconcile_at = time.time()
        self._position_reconcile_stale = False
        self.mark_positions_changed()
        await self._publish_runtime_heartbeat(client)
        return True

    async def reconcile_positions(self, redis_client=None) -> bool:
        owned_client = redis_client is None
        client = redis_client or await self._connect_redis(self.config.REDIS_URL)
        try:
            local = self._local_positions_by_id()
            raw = client.get(f"paper:positions:snapshot:{self.config.ALPHA_ID}")
            snapshot = parse_snapshot(raw)
            if snapshot is None:
                if self.config.RECONCILE_NO_POSITION_IS_OK and not self._has_open_positions():
                    return await self._accept_empty_position_reconcile(client)
                self._position_reconcile_stale = True
                return False
            age = snapshot_age_sec(snapshot)
            self._snapshot_age_sec = age
            revision = int(snapshot.get("revision", 0))
            snapshot_positions = snapshot.get("positions")
            if not isinstance(snapshot_positions, list):
                snapshot_positions = []
            authoritative = {
                str(pos["position_id"]): pos for pos in snapshot_positions
                if isinstance(pos, dict) and pos.get("position_id")
            }
            open_positions = getattr(self, "_open_positions", None)
            if not authoritative:
                if isinstance(open_positions, dict):
                    open_positions.clear()
                return await self._accept_empty_position_reconcile(client, revision, age)

            snapshot_stale = age > float(self.config.POSITION_SNAPSHOT_MAX_AGE_SEC)
            if snapshot_stale and self.config.RECONCILE_STALE_SUSPEND_NEW_ENTRIES:
                self._position_reconcile_stale = True
            else:
                self._position_reconcile_stale = False

            if not isinstance(open_positions, dict):
                if authoritative:
                    self._position_reconcile_stale = True
                return not authoritative

            for position_id, (symbol, _) in list(local.items()):
                if position_id not in authoritative:
                    open_positions.pop(symbol, None)
            for position_id, raw_position in authoritative.items():
                restored = self.restore_position(raw_position)
                if restored is None:
                    if snapshot_stale:
                        continue
                    self._position_reconcile_stale = True
                    return False
                symbol = restored["symbol"]
                mode = "RESTORED" if position_id not in local else "AUTHORITATIVE_REFRESH"
                metadata = raw_position.get("metadata")
                if not isinstance(metadata, dict) or not isinstance(metadata.get("strategy_runtime"), dict):
                    mode = "RECOVERED_CONSERVATIVE"
                open_positions[symbol] = restored
                self.on_position_reconciled(restored, mode)

            self._authoritative_revision = revision
            self._authoritative_position_ids = set(authoritative)
            self._last_reconcile_at = time.time()
            self.mark_positions_changed()
            await self._publish_runtime_heartbeat(client)
            return True
        finally:
            if owned_client:
                client.close()

    async def _publish_runtime_heartbeat(self, redis_client=None) -> None:
        owned_client = redis_client is None
        client = redis_client or await self._connect_redis(self.config.REDIS_URL)
        positions = self._local_positions_by_id()
        symbols = self._get_active_position_symbols()
        payload = {
            "alpha_id": self.config.ALPHA_ID,
            "authoritative_revision": self._authoritative_revision,
            "managed_position_ids": sorted(positions),
            "managed_symbols": sorted(symbols),
            "desired_price_alert_symbols": sorted(symbols),
            "runtime_state": self.runtime_state,
            "reconciled_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            client.set(
                f"paper:alpha-runtime:{self.config.ALPHA_ID}",
                json_dumps(payload),
                ex=int(self.config.ALPHA_RUNTIME_HEARTBEAT_TTL_SEC),
            )
            self._heartbeat_ok = True
        except Exception:
            self._heartbeat_ok = False
            raise
        finally:
            if owned_client:
                client.close()

    async def _position_reconcile_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                ok = await self.reconcile_positions()
                if not ok:
                    self._position_reconcile_stale = True
                    self._logger.warning("[%s] Reconcile failed, new entries suspended", self.config.ALPHA_ID)
                elif not self._position_reconcile_stale:
                    self._logger.debug("[%s] Position reconcile healthy", self.config.ALPHA_ID)
            except Exception as exc:
                self._position_reconcile_stale = True
                self._logger.warning(
                    "[%s] Position reconcile failed: %s. New entries suspended",
                    self.config.ALPHA_ID, exc,
                )
            await asyncio.sleep(float(self.config.POSITION_RECONCILE_INTERVAL_SEC))

    async def _startup_reconcile(self) -> bool:
        deadline = asyncio.get_running_loop().time() + float(
            self.config.POSITION_RECONCILE_STARTUP_TIMEOUT_SEC
        )
        while not self.shutdown_event.is_set():
            try:
                if await self.reconcile_positions():
                    return True
            except Exception as exc:
                self._logger.warning("[%s] Startup reconcile failed: %s", self.config.ALPHA_ID, exc)
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(min(1.0, float(self.config.POSITION_RECONCILE_INTERVAL_SEC)))
        return False

    def _ownership_healthy(self) -> bool:
        return (
            self.runtime_state == "LIVE"
            and self._snapshot_age_sec <= float(self.config.POSITION_SNAPSHOT_MAX_AGE_SEC)
            and set(self._local_positions_by_id()) == self._authoritative_position_ids
            and self._heartbeat_ok
            and self._get_active_position_symbols() <= self._subscribed_price_alert_symbols
        )

    async def manage_loop(self) -> None:
        while not self.shutdown_event.is_set():
            await asyncio.sleep(self.config.MANAGE_INTERVAL_SEC)
            if not self._has_open_positions():
                continue
            try:
                await self._manage_positions()
            except Exception as exc:
                self._logger.error("Manage error: %s", exc, exc_info=True)

    @property
    def _mds_url(self) -> str:
        """Redis URL for market data (kline pub/sub + warmup). Falls back to REDIS_URL."""
        return self.config.MDS_REDIS_URL or self.config.REDIS_URL

    def _kline_channel(self, tf: str) -> str:
        """Build kline channel name respecting external MDS exchange prefix."""
        exchange = self._mds_exchange()
        return f"kline:{exchange}:{tf}" if exchange else f"kline:{tf}"

    def _warmup_stream(self) -> str:
        """Warmup request stream name — exchange-specific for external MDS."""
        exchange = self._mds_exchange()
        return f"warmup:request:{exchange}" if exchange else "warmup:request"

    def _mds_exchange(self) -> str:
        return str(getattr(self.config, "MDS_EXCHANGE", "") or "").strip()

    def _price_alert_subscribe_channel(self) -> str | None:
        exchange = self._mds_exchange()
        return f"price_alert:subscribe:{exchange}" if exchange else None

    def _price_alert_channel(self, symbol: str) -> str | None:
        exchange = self._mds_exchange()
        return f"price_alert:{exchange}:{symbol}" if exchange and symbol else None

    def on_ticker_message(self, msg: dict) -> None:
        """Called for every ticker update from MDS (bookTicker mid-price).
        Override in subclasses to manage SL/TP on real-time price ticks.
        msg = {"symbol": "BTCUSDT", "price": 65432.1, ...}
        """

    def on_price_alert_message(self, msg: dict) -> None:
        """Called for every side-aware price_alert tick from MDS."""

    def on_symbol_universe_message(self, msg: dict) -> None:
        """Called when MDS publishes the symbol universe for an exchange.
        msg = {"symbols": ["BTCUSDT", "ETHUSDT", ...]}
        Caches the universe so warmup reconnects avoid repeated Binance API calls.
        Override to extend behavior.
        """
        symbols = msg.get("symbols", [])
        if symbols:
            self._symbol_universe_cache = [s for s in symbols if not self._is_blacklisted(s)]
            self._logger.info(
                "[%s] Symbol universe cached: %d symbols from MDS",
                self.config.ALPHA_ID, len(self._symbol_universe_cache),
            )

    @staticmethod
    def _tf_to_ms(tf: str) -> int:
        if tf.endswith("m"):
            return int(tf[:-1]) * 60_000
        if tf.endswith("h"):
            return int(tf[:-1]) * 3_600_000
        if tf.endswith("d"):
            return int(tf[:-1]) * 86_400_000
        return 60_000

    @classmethod
    def _tf_to_seconds(cls, tf: str) -> float:
        return cls._tf_to_ms(tf) / 1000.0

    def _max_subscribed_kline_interval_sec(self) -> float:
        intervals = []
        for channel in self._build_channels():
            if not channel.startswith("kline:"):
                continue
            tf = channel.rsplit(":", 1)[-1]
            intervals.append(self._tf_to_seconds(tf))
        return max(intervals, default=0.0)

    def _stale_threshold_seconds(self) -> float:
        price_alert_threshold = self.config.PRICE_ALERT_STALE_SEC * 8
        kline_threshold = self._max_subscribed_kline_interval_sec() * 2.5
        return max(120.0, price_alert_threshold, kline_threshold)

    def _tf_seconds(self, tf: str) -> float:
        unit = tf[-1:] if tf else ""
        try:
            value = int(tf[:-1])
        except (TypeError, ValueError):
            return float(self.config.RECONNECT_WARMUP_SKIP_IF_FRESH_SEC)
        if unit == "m":
            return value * 60.0
        if unit == "h":
            return value * 3600.0
        if unit == "d":
            return value * 86400.0
        return float(self.config.RECONNECT_WARMUP_SKIP_IF_FRESH_SEC)

    def _reconnect_warmup_freshness_sec(self, tf: str) -> float:
        configured = float(self.config.RECONNECT_WARMUP_SKIP_IF_FRESH_SEC)
        if configured <= 0:
            return 0.0
        return max(configured, self._tf_seconds(tf) * 2.0)

    def _should_skip_reconnect_warmup(self, tf: str) -> bool:
        max_age = self._reconnect_warmup_freshness_sec(tf)
        if max_age <= 0:
            return False

        symbols = list(getattr(self, "symbols", None) or self._get_warmup_symbols())
        if not symbols:
            return False

        required_coverage = float(getattr(self.config, "WARMUP_MIN_SYMBOL_COVERAGE", 1.0))
        if required_coverage > 1:
            required_coverage = required_coverage / 100.0
        required_coverage = max(0.0, min(1.0, required_coverage))
        required = max(1, ceil(len(symbols) * required_coverage))
        now = time.time()
        fresh = 0

        for symbol in symbols:
            sd = self.symbol_data.get(symbol, {}).get(tf)
            if not sd or not sd.time_list:
                continue
            latest_sec = float(sd.time_list[-1]) / 1000.0
            if now - latest_sec <= max_age:
                fresh += 1

        if fresh < required:
            return False

        if self.last_redis_message_at > 0:
            redis_gap = now - self.last_redis_message_at
            if redis_gap > max(max_age, self._stale_threshold_seconds()):
                return False

        return True

    def _stale_should_break_listener(self) -> bool:
        return bool(self._transport_reconnect_requested)

    def _check_data_stale_recovery(self) -> None:
        if not self._data_stale:
            return
        if self.last_redis_message_at <= 0:
            return
        gap = time.time() - self.last_redis_message_at
        if gap < self._stale_threshold_seconds():
            self._data_stale = False
            self._logger.info("[%s] Market data resumed; data stale flag cleared", self.config.ALPHA_ID)
            if (
                self.runtime_state == "STALE"
                and not self._data_stale
                and not self._position_reconcile_stale
                and not self._price_alert_sync_stale
            ):
                self.runtime_state = "LIVE"

    def _build_channels(self) -> list[str]:
        # Translate bare "kline:{tf}" from get_required_channels() to the
        # correct exchange-prefixed channel if external MDS is configured.
        raw = self.get_required_channels()
        channels = []
        for ch in raw:
            if ch.startswith("kline:"):
                tf = ch.rsplit(":", 1)[-1]
                channels.append(self._kline_channel(tf))
            else:
                channels.append(ch)

        exchange = self._mds_exchange()
        if exchange:
            # P3.11: subscribe symbol universe channel to detect MDS restarts
            symbols_ch = f"symbols:{exchange}"
            if symbols_ch not in channels:
                channels.append(symbols_ch)
        else:
            # Internal/legacy MDS: fall back to 1m kline for position management
            manage_1m = self._kline_channel("1m")
            if self.config.MANAGE_INTERVAL_SEC > 0 and manage_1m not in channels:
                channels.append(manage_1m)
        return channels

    def _is_blacklisted(self, symbol: str) -> bool:
        return symbol in self._blacklist

    def _get_active_position_symbols(self) -> set[str]:
        positions = getattr(self, "_open_positions", {})
        if isinstance(positions, dict):
            return {str(symbol) for symbol in positions.keys() if symbol}
        return set()

    def _claim_position_candle(self, position: dict, candle_open_ms: int) -> bool:
        """Claim one closed strategy candle for position management.

        The entry candle predates the position and must never drive an exit. A
        repeated latest candle must also be ignored when MDS has not published
        the next timeframe candle before the alpha's boundary scan.
        """
        try:
            candle_open_ms = int(candle_open_ms)
            entry_open_ms = int(position.get("entry_candle_open_ms", 0) or 0)
            entry_close_ms = int(position.get("signal_candle_close_ms", 0) or 0)
            last_processed_ms = int(
                position.get(
                    "last_strategy_candle_ms",
                    entry_open_ms if entry_open_ms > 0 else entry_close_ms - 1,
                )
                or 0
            )
        except (TypeError, ValueError):
            return False

        if candle_open_ms <= 0:
            return False
        if entry_close_ms > 0 and candle_open_ms < entry_close_ms:
            return False
        if entry_close_ms <= 0 and entry_open_ms > 0 and candle_open_ms <= entry_open_ms:
            return False
        if candle_open_ms <= last_processed_ms:
            return False

        position["last_strategy_candle_ms"] = candle_open_ms
        return True

    def mark_positions_changed(self) -> None:
        self._positions_changed.set()

    def _trigger_price(self, side: str, tick: dict) -> float | None:
        keys = ("bid", "last", "price") if side.upper() == "LONG" else ("ask", "last", "price")
        for key in keys:
            value = tick.get(key)
            if value is None:
                continue
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            if price > 0:
                return price
        return None

    def _build_candle_close_metadata(
        self,
        *,
        reason: str,
        stop_price: float,
        trigger_price: float,
        fill_price: float,
        candle_high: float,
        candle_low: float,
        tf: str = "1m",
    ) -> str:
        return json_dumps({
            "close_model": "candle_fallback_conservative",
            "reason": reason,
            "stop_price": stop_price,
            "trigger_price": trigger_price,
            "raw_fill_price": fill_price,
            "candle_high": candle_high,
            "candle_low": candle_low,
            "tf": tf,
            "source": "kline",
        })

    def _build_close_metadata(
        self,
        *,
        reason: str,
        stop_price: float,
        trigger_price: float,
        tick: dict,
        close_model: str = "price_alert_side_aware",
    ) -> str:
        return json_dumps({
            "close_model": close_model,
            "reason": reason,
            "stop_price": stop_price,
            "trigger_price": trigger_price,
            "raw_fill_price": trigger_price,
            "bid": tick.get("bid"),
            "ask": tick.get("ask"),
            "last": tick.get("last"),
            "price": tick.get("price"),
            "tick_timestamp": tick.get("timestamp"),
            "source": tick.get("source"),
            "ref_is_executable": close_model == "price_alert_side_aware",
        })

    def can_open_new_trades(self) -> bool:
        return (
            self.runtime_state == "LIVE"
            and not self._data_stale
            and not self._position_reconcile_stale
            and not self._price_alert_sync_stale
        )

    def is_symbol_data_ready(self, symbol: str, tf: str) -> bool:
        tf_map = self.symbol_data.get(symbol)
        if not tf_map:
            return False
        sd = tf_map.get(tf)
        return bool(sd and sd.price_list)

    def _warmup_coverage(self, ready_count: int, total_count: int) -> tuple[bool, int, float]:
        min_coverage = float(getattr(self.config, "WARMUP_MIN_SYMBOL_COVERAGE", 0.90))
        if min_coverage > 1:
            min_coverage = min_coverage / 100.0
        min_coverage = max(0.0, min(1.0, min_coverage))
        required_symbols = max(1, ceil(total_count * min_coverage))
        return ready_count >= required_symbols, required_symbols, min_coverage

    def is_position_price_ready(self, symbol: str) -> bool:
        exchange = self._mds_exchange()
        if not exchange:
            return True
        last = self.last_price_alert_at.get(exchange, {}).get(symbol, 0.0)
        return time.time() - last < self.config.PRICE_ALERT_STALE_SEC

    def on_kline_message(self, msg: dict) -> None:
        symbol = msg.get("symbol", "")
        tf = msg.get("tf", "")
        exchange = self._mds_exchange()
        msg_exchange = str(msg.get("exchange", "") or "")
        if exchange and msg_exchange and msg_exchange != exchange:
            return
        if not symbol or not tf:
            return
        try:
            open_time = int(msg.get("open_time", 0))
        except (TypeError, ValueError):
            return
        if open_time <= 0:
            return

        if self._is_blacklisted(symbol):
            return

        if symbol not in self.symbol_data:
            self.symbol_data[symbol] = {}
        if tf not in self.symbol_data[symbol]:
                self.symbol_data[symbol][tf] = SymbolData()

        sd = self.symbol_data[symbol][tf]
        self._upsert_candle(sd, msg, open_time)
        self.last_kline_at.setdefault(exchange or "default", {}).setdefault(tf, {})[symbol] = time.time()
        self.last_redis_message_at = time.time()
        self._trim_symbol_data(sd)

    def _load_warmup_candles(self, data: dict) -> bool:
        symbol = data.get("symbol", "")
        tf = data.get("tf", "")
        if not symbol or not tf:
            return False
        if self._is_blacklisted(symbol):
            return False

        candles_raw = data.get("candles", "[]")
        candles = json_loads(candles_raw) if isinstance(candles_raw, str) else candles_raw
        if not candles:
            return False

        if symbol not in self.symbol_data:
            self.symbol_data[symbol] = {}
        if tf not in self.symbol_data[symbol]:
            self.symbol_data[symbol][tf] = SymbolData()

        sd = self.symbol_data[symbol][tf]
        for candle in candles:
            try:
                open_time = int(candle.get("open_time", 0))
            except (TypeError, ValueError):
                continue
            if open_time <= 0:
                continue
            self._upsert_candle(sd, candle, open_time)

        self._trim_symbol_data(sd)
        return bool(sd.time_list)

    def _make_warmup_request_id(self, tf: str) -> str:
        exchange = self._mds_exchange()
        return f"{self.config.ALPHA_ID}:warmup:{exchange or 'default'}:{tf}:{uuid.uuid4().hex}"

    async def _try_snapshot_warmup(
        self,
        redis_client: redis_lib.Redis,
        symbols: list[str],
        tf: str,
        required_bars: int,
    ) -> set[str]:
        """Load candles from MDS Redis snapshots.

        Prefer the current LIST snapshot
        ``kline_snapshot_v2:{exchange}:{tf}:{symbol}`` (newest first), then
        fall back to the legacy HASH snapshot. MDS cache size is configurable
        and currently supports large warmups, e.g. up to 12000 bars.
        Freshness check: latest open_time must be within 2 candle durations of now.
        Returns set of symbols successfully loaded.
        """
        exchange = self._mds_exchange()
        if not exchange:
            return set()

        now_ms = int(time.time() * 1000)
        candle_ms = self._tf_to_ms(tf)
        stale_threshold_ms = candle_ms * 2

        loaded: set[str] = set()
        for symbol in symbols:
            list_key = f"kline_snapshot_v2:{exchange}:{tf}:{symbol}"
            candles = []
            try:
                values = redis_client.lrange(list_key, 0, required_bars - 1)
            except Exception:
                values = []

            for candle_json in values or []:
                try:
                    candles.append(json_loads(candle_json))
                except Exception:
                    continue

            if len(candles) < required_bars:
                legacy_key = f"kline_snapshot:{exchange}:{tf}:{symbol}"
                try:
                    raw: dict = redis_client.hgetall(legacy_key)
                except Exception:
                    raw = {}

                for candle_json in raw.values():
                    try:
                        candles.append(json_loads(candle_json))
                    except Exception:
                        continue

            if len(candles) < required_bars:
                continue

            candles.sort(key=lambda c: int(c.get("open_time", 0)))
            latest_open_time = int(candles[-1].get("open_time", 0))

            if now_ms - latest_open_time > stale_threshold_ms:
                self._logger.debug(
                    "[%s] Snapshot stale for %s/%s: age=%ds threshold=%ds",
                    self.config.ALPHA_ID, symbol, tf,
                    (now_ms - latest_open_time) // 1000,
                    stale_threshold_ms // 1000,
                )
                continue

            ok = self._load_warmup_candles({
                "symbol": symbol,
                "tf": tf,
                "candles": json_dumps(candles[-required_bars:]),
            })
            if ok:
                loaded.add(symbol)

        return loaded

    async def _discover_snapshot_symbols(self, redis_client: redis_lib.Redis, tf: str) -> list[str]:
        """Scan MDS Redis for available snapshot keys; returns symbol list without API calls."""
        exchange = self._mds_exchange()
        if not exchange:
            return []
        symbols: list[str] = []
        pattern = f"kline_snapshot_v2:{exchange}:{tf}:*"
        prefix_len = len(f"kline_snapshot_v2:{exchange}:{tf}:")
        cursor = 0
        while True:
            cursor, keys = await asyncio.to_thread(redis_client.scan, cursor, match=pattern, count=500)
            for key in keys:
                symbol = key[prefix_len:]
                if symbol and not self._is_blacklisted(symbol):
                    symbols.append(symbol)
            if cursor == 0:
                break

        legacy_pattern = f"kline_snapshot:{exchange}:{tf}:*"
        legacy_prefix_len = len(f"kline_snapshot:{exchange}:{tf}:")
        cursor = 0
        while True:
            cursor, keys = await asyncio.to_thread(redis_client.scan, cursor, match=legacy_pattern, count=500)
            for key in keys:
                symbol = key[legacy_prefix_len:]
                if symbol and not self._is_blacklisted(symbol):
                    symbols.append(symbol)
            if cursor == 0:
                break
        return sorted(set(symbols))

    async def _request_warmup(self) -> bool:
        all_symbols = self._get_warmup_symbols()
        tf = getattr(self.config, "TF", "")
        bars = self.config.WARMUP_BARS
        exchange = self._mds_exchange()
        snapshot_only = False  # when True, skip stream fallback

        if not all_symbols:
            if self._symbol_universe_cache:
                all_symbols = self._symbol_universe_cache
                snapshot_only = True
                self._logger.info(
                    "[%s] Symbol fetch failed, using cached MDS universe: %d symbols (snapshot-only)",
                    self.config.ALPHA_ID, len(all_symbols),
                )
            elif exchange:
                # Last resort: discover symbols directly from MDS snapshot keys
                rc = await self._connect_redis(self._mds_url)
                try:
                    all_symbols = await self._discover_snapshot_symbols(rc, tf)
                finally:
                    rc.close()
                if all_symbols:
                    snapshot_only = True
                    self._logger.info(
                        "[%s] Symbol fetch failed, discovered %d symbols from MDS snapshots (snapshot-only)",
                        self.config.ALPHA_ID, len(all_symbols),
                    )
                else:
                    self._logger.warning("[%s] No warmup symbols, skipping warmup", self.config.ALPHA_ID)
                    return False
            else:
                self._logger.warning("[%s] No warmup symbols, skipping warmup", self.config.ALPHA_ID)
                return False

        received_symbols: set[str] = set()

        # Snapshot fast path: when MDS has already backfilled Redis history,
        # skip the warmup stream and avoid repeated exchange REST calls.
        if exchange:
            rc = await self._connect_redis(self._mds_url)
            try:
                snapshot_loaded = await self._try_snapshot_warmup(rc, all_symbols, tf, bars)
                received_symbols |= snapshot_loaded
            finally:
                rc.close()
            if snapshot_loaded:
                self._logger.info(
                    "[%s] Snapshot fast path: %d/%d symbols loaded",
                    self.config.ALPHA_ID, len(snapshot_loaded), len(all_symbols),
                )

        stream_symbols = [] if snapshot_only else [s for s in all_symbols if s not in received_symbols]

        if stream_symbols:
            request_id = self._make_warmup_request_id(tf)
            redis_client = await self._connect_redis(self._mds_url)
            try:
                response_stream = f"warmup:response:{request_id}"
                redis_client.xadd(
                    self._warmup_stream(),
                    {
                        "alpha_id": request_id,
                        "tf": tf,
                        "bars": str(bars),
                        "symbols": ",".join(stream_symbols),
                    },
                )

                timeout_sec = float(getattr(self.config, "INITIAL_DATA_TIMEOUT_SEC", 30.0))
                deadline = asyncio.get_running_loop().time() + timeout_sec
                expected_stream = set(stream_symbols)
                last_response_id = "0-0"

                while expected_stream - received_symbols:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        self._logger.warning(
                            "[%s] Warmup timeout: %d/%d symbols received",
                            self.config.ALPHA_ID,
                            len(received_symbols),
                            len(all_symbols),
                        )
                        break

                    messages = redis_client.xread(
                        {response_stream: last_response_id},
                        count=len(expected_stream),
                        # Redis interprets BLOCK 0 as "wait forever".
                        block=max(50, int(min(remaining, 5) * 1000)),
                    )

                    if not messages:
                        # Real Redis blocks, but mocks/proxies may return immediately.
                        await asyncio.sleep(min(0.05, max(remaining, 0)))
                        continue

                    for _stream, entries in messages:
                        for msg_id, fields in entries:
                            last_response_id = msg_id
                            loaded = self._load_warmup_candles(fields)
                            sym = fields.get("symbol", "")
                            if sym and loaded:
                                received_symbols.add(sym)

                try:
                    redis_client.delete(response_stream)
                except Exception:
                    pass
            finally:
                redis_client.close()

        ready_symbols = {
            symbol
            for symbol in all_symbols
            if self.is_symbol_data_ready(symbol, tf)
        }
        ready_count = len(ready_symbols)
        ok, required_symbols, min_coverage = self._warmup_coverage(ready_count, len(all_symbols))
        if received_symbols:
            now = time.time()
            for sym in received_symbols:
                self.last_warmup_ok_at.setdefault(exchange or "default", {}).setdefault(tf, {})[sym] = now
        if not ok:
            missing = sorted(set(all_symbols) - ready_symbols)
            self._logger.warning(
                "[%s] Warmup incomplete: ready=%d/%d required=%d coverage=%.2f%% required_coverage=%.2f%% received_this_round=%d missing_sample=%s",
                self.config.ALPHA_ID,
                ready_count,
                len(all_symbols),
                required_symbols,
                (ready_count / len(all_symbols)) * 100.0,
                min_coverage * 100.0,
                len(received_symbols),
                missing[:10],
            )
        elif ready_count < len(all_symbols):
            missing = sorted(set(all_symbols) - ready_symbols)
            self._logger.warning(
                "[%s] Warmup coverage accepted: ready=%d/%d required=%d coverage=%.2f%% required_coverage=%.2f%% missing_sample=%s",
                self.config.ALPHA_ID,
                ready_count,
                len(all_symbols),
                required_symbols,
                (ready_count / len(all_symbols)) * 100.0,
                min_coverage * 100.0,
                missing[:10],
            )
        log_message = (
            "[%s] Warmup complete: %d/%d symbols loaded (required=%d, coverage=%.2f%%, required_coverage=%.2f%%)"
            if ok
            else "[%s] Warmup finished below coverage: %d/%d symbols loaded (required=%d, coverage=%.2f%%, required_coverage=%.2f%%)"
        )
        self._logger.info(
            log_message,
            self.config.ALPHA_ID,
            ready_count,
            len(all_symbols),
            required_symbols,
            (ready_count / len(all_symbols)) * 100.0,
            min_coverage * 100.0,
        )
        return ok

    async def subscribe_data_feeds(self) -> asyncio.Task:
        async def _listen() -> None:
            connect_count = 0
            while not self.shutdown_event.is_set():
                redis_client = await self._connect_redis(self._mds_url)
                pubsub = redis_client.pubsub()
                self._subscribed_price_alert_symbols = set()
                channels = self._build_channels()

                async def _refresh_price_alert_subscriptions() -> None:
                    desired = self._get_active_position_symbols()
                    to_subscribe = desired - self._subscribed_price_alert_symbols
                    to_unsubscribe = self._subscribed_price_alert_symbols - desired

                    sub_channels = [ch for sym in sorted(to_subscribe) if (ch := self._price_alert_channel(sym))]
                    unsub_channels = [ch for sym in sorted(to_unsubscribe) if (ch := self._price_alert_channel(sym))]

                    if sub_channels:
                        await asyncio.to_thread(pubsub.subscribe, *sub_channels)
                    if unsub_channels:
                        await asyncio.to_thread(pubsub.unsubscribe, *unsub_channels)

                    self._subscribed_price_alert_symbols = set(desired)

                try:
                    await asyncio.to_thread(pubsub.subscribe, *channels)
                    connect_count += 1
                    self._logger.info("[%s] Subscribed to data channels: %s", self.config.ALPHA_ID, channels)
                    await _refresh_price_alert_subscriptions()
                    self._publish_price_alert_sync(redis_client, self._get_active_position_symbols())

                    # On reconnect (not the initial startup connect), fill gaps only
                    # when the local cache is not already fresh enough.
                    if connect_count > 1:
                        self.runtime_state = "RECOVERING"
                        tf = getattr(self.config, "TF", "")
                        try:
                            if self._should_skip_reconnect_warmup(tf):
                                warmup_ok = True
                                self._logger.info(
                                    "[%s] Reconnect: cache fresh enough, skipping warmup",
                                    self.config.ALPHA_ID,
                                )
                            else:
                                warmup_ok = await self._request_warmup()
                            if not warmup_ok:
                                self._data_stale = True
                        except Exception as exc:
                            self._logger.warning("[%s] Rewarmup error: %s", self.config.ALPHA_ID, exc)
                            self._data_stale = True
                        if (
                            not self._data_stale
                            and not self._position_reconcile_stale
                            and not self._price_alert_sync_stale
                        ):
                            self.runtime_state = "LIVE"
                        else:
                            self.runtime_state = "STALE"

                    while not self.shutdown_event.is_set():
                        if self._stale_should_break_listener():
                            self._transport_reconnect_requested = False
                            break
                        if self._positions_changed.is_set():
                            self._positions_changed.clear()
                            await _refresh_price_alert_subscriptions()
                            self._publish_price_alert_sync(redis_client, self._get_active_position_symbols())
                        msg = await asyncio.to_thread(pubsub.get_message, timeout=1.0)
                        if not msg or msg["type"] != "message":
                            continue
                        channel = msg["channel"]
                        if isinstance(channel, bytes):
                            channel = channel.decode()
                        data = json_loads(msg["data"])
                        self.last_redis_message_at = time.time()
                        self._check_data_stale_recovery()
                        if channel.startswith("kline:"):
                            self.on_kline_message(data)
                        elif channel.startswith("price_alert:"):
                            self._handle_price_alert_message(data)
                        elif channel.startswith("symbols:"):
                            self.on_symbol_universe_message(data)
                except redis_lib.RedisError as exc:
                    self.runtime_state = "STALE"
                    self._logger.warning("[%s] Redis Pub/Sub error: %s. Reconnecting.", self.config.ALPHA_ID, exc)
                    await asyncio.sleep(1)
                except Exception as exc:
                    self._logger.debug("Redis subscriber error: %s", exc)
                    await asyncio.sleep(1)
                finally:
                    try:
                        pubsub.unsubscribe()
                    except Exception:
                        pass
                    pubsub.close()
                    redis_client.close()

        return asyncio.create_task(_listen())

    def _handle_price_alert_message(self, data: dict) -> None:
        symbol = str(data.get("symbol", "") or "")
        exchange = self._mds_exchange()
        msg_exchange = str(data.get("exchange", "") or "")
        if not symbol:
            return
        if exchange and msg_exchange and msg_exchange != exchange:
            return
        now = time.time()
        self.last_price_alert_at.setdefault(exchange or "default", {})[symbol] = now
        self.latest_price_alert[symbol] = data
        self.on_price_alert_message(data)

    def _publish_price_alert_sync(self, redis_client: redis_lib.Redis, symbols: set[str]) -> None:
        channel = self._price_alert_subscribe_channel()
        if not channel:
            return
        payload = {
            "consumer_id": self.config.ALPHA_ID,
            "action": "sync",
            "symbols": sorted(symbols),
        }
        redis_client.publish(channel, json_dumps(payload))

    def _mark_price_alert_sync_failed(self, exc: Exception) -> None:
        if self.config.PRICE_ALERT_SYNC_SUSPEND_NEW_ENTRIES:
            self._price_alert_sync_stale = True
        self._logger.warning(
            "[%s] Price alert sync failed: %s. New entries suspended.",
            self.config.ALPHA_ID, exc,
        )

    def _mark_price_alert_sync_recovered(self) -> None:
        if self._price_alert_sync_stale:
            self._logger.info("[%s] Price alert sync recovered", self.config.ALPHA_ID)
        self._price_alert_sync_stale = False

    async def _price_alert_sync_loop(self) -> None:
        if not self._price_alert_subscribe_channel():
            return

        redis_client = await self._connect_redis(self._mds_url)
        interval = float(self.config.PRICE_ALERT_SYNC_INTERVAL_SEC)
        try:
            while not self.shutdown_event.is_set():
                try:
                    self._publish_price_alert_sync(redis_client, self._get_active_position_symbols())
                    self._mark_price_alert_sync_recovered()
                except redis_lib.RedisError as exc:
                    self._mark_price_alert_sync_failed(exc)
                    redis_client.close()
                    redis_client = await self._connect_redis(self._mds_url)
                await asyncio.sleep(interval)
        finally:
            try:
                self._publish_price_alert_sync(redis_client, set())
            except Exception:
                pass
            redis_client.close()

    async def _stale_monitor_loop(self) -> None:
        """Marks market data stale when no Redis message arrives within the stale window.

        Only active in external MDS mode (MDS_EXCHANGE is set). Uses a conservative
        threshold (8x PRICE_ALERT_STALE_SEC, min 120s) to avoid false positives from
        quiet market periods. The listener stays attached so fresh data can recover
        without forcing a reconnect + rewarmup loop.
        """
        if not self._mds_exchange():
            return
        stale_threshold = self._stale_threshold_seconds()
        while not self.shutdown_event.is_set():
            await asyncio.sleep(30)
            if self.last_redis_message_at <= 0:
                continue
            gap = time.time() - self.last_redis_message_at
            if gap > stale_threshold:
                if self.runtime_state == "LIVE" or not self._data_stale:
                    self._data_stale = bool(self.config.DATA_STALE_SUSPEND_NEW_ENTRIES)
                    self.runtime_state = "STALE"
                    self._logger.warning(
                        "[%s] Stale: no Redis message for %.0fs (threshold=%.0fs). "
                        "New entries suspended, continuing to listen for data.",
                        self.config.ALPHA_ID, gap, stale_threshold,
                    )
            else:
                self._check_data_stale_recovery()

    async def _publish_empty_price_alert_sync(self) -> None:
        if not self._price_alert_subscribe_channel():
            return
        redis_client = redis_lib.from_url(self._mds_url, decode_responses=True)
        try:
            await asyncio.to_thread(redis_client.ping)
            self._publish_price_alert_sync(redis_client, set())
        finally:
            redis_client.close()

    async def _connect_redis(self, url: str | None = None) -> redis_lib.Redis:
        target_url = url or self.config.REDIS_URL
        attempt = 0
        while not self.shutdown_event.is_set():
            attempt += 1
            redis_client = redis_lib.from_url(
                target_url,
                decode_responses=True,
                socket_timeout=None,
            )
            try:
                redis_client.ping()
                return redis_client
            except redis_lib.RedisError as exc:
                redis_client.close()
                wait = min(attempt, 10)
                self._logger.warning("Redis [%s] unavailable: %s. Retry in %ss", target_url, exc, wait)
                await asyncio.sleep(wait)
        raise asyncio.CancelledError

    async def run(self) -> None:
        os.makedirs(self.config.LOG_DIR, exist_ok=True)
        app_level = getattr(logging, self.config.LOG_LEVEL.upper(), logging.INFO)
        handlers = [
            logging.StreamHandler(),
            logging.FileHandler(os.path.join(self.config.LOG_DIR, "bot.log")),
        ]
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        for h in handlers:
            h.setFormatter(fmt)
        root = logging.getLogger()
        root.setLevel(logging.WARNING)
        root.handlers = handlers
        for name in (self.config.ALPHA_ID, "base", "app", "__main__"):
            logging.getLogger(name).setLevel(app_level)

        signal_push.init(self.config.REDIS_URL, self.config.REDIS_STREAM)

        if self._columns_config_path:
            self.load_columns_config(self._columns_config_path)

        loop = asyncio.get_running_loop()
        for signal_name in (sig.SIGTERM, sig.SIGINT):
            loop.add_signal_handler(signal_name, self.shutdown_event.set)

        self._logger.info("[%s] Starting alpha engine", self.config.ALPHA_ID)
        try:
            os.remove("/tmp/bot_health")
        except FileNotFoundError:
            pass

        try:
            self.runtime_state = "WARMING_UP"
            warmup_ok = await self._request_warmup()
            self._data_stale = not warmup_ok
            self.runtime_state = "LIVE" if warmup_ok else "STALE"
        except Exception as exc:
            self._data_stale = True
            self.runtime_state = "STALE"
            self._logger.warning("[%s] Warmup failed: %s", self.config.ALPHA_ID, exc)

        try:
            await self.on_warmup_complete()
        except Exception as exc:
            self._logger.warning("[%s] on_warmup_complete error: %s", self.config.ALPHA_ID, exc)

        reconcile_ok = await self._startup_reconcile()
        if not reconcile_ok:
            self._position_reconcile_stale = True
            self.runtime_state = "STALE"

        sub_task = await self.subscribe_data_feeds()

        timeout_sec = float(getattr(self.config, "INITIAL_DATA_TIMEOUT_SEC", 30.0))
        deadline = asyncio.get_running_loop().time() + timeout_sec
        while len(self.symbol_data) == 0 and not self.shutdown_event.is_set():
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(1)

        tf = getattr(self.config, "TF", "")
        if self.runtime_state != "LIVE":
            self._logger.warning(
                "[%s] Not ready: runtime_state=%s, loaded_symbols=%d; waiting for warmup coverage.",
                self.config.ALPHA_ID,
                self.runtime_state,
                len(self.symbol_data),
            )
        elif len(self.symbol_data) == 0:
            self._logger.warning(
                "[%s] No market data after %.1fs; starting scan loop and waiting for live data.",
                self.config.ALPHA_ID,
                timeout_sec,
            )
        else:
            total_candles = sum(
                len(tf_map.get(tf, SymbolData()).price_list)
                for tf_map in self.symbol_data.values()
            )
            self._logger.info(
                "[%s] Ready: %d symbols, %d candles at %s",
                self.config.ALPHA_ID,
                len(self.symbol_data),
                total_candles,
                tf,
            )

        scan_task = asyncio.create_task(self.scan_loop())
        manage_task = asyncio.create_task(self.manage_loop())
        health_task = asyncio.create_task(self._health_loop())
        price_alert_sync_task = asyncio.create_task(self._price_alert_sync_loop())
        stale_task = asyncio.create_task(self._stale_monitor_loop())
        reconcile_task = asyncio.create_task(self._position_reconcile_loop())

        try:
            await asyncio.gather(scan_task, manage_task, health_task, sub_task, price_alert_sync_task, stale_task, reconcile_task)
        except asyncio.CancelledError:
            pass
        finally:
            self.runtime_state = "SHUTTING_DOWN"
            self.shutdown_event.set()
            for task in (scan_task, manage_task, health_task, sub_task, price_alert_sync_task, stale_task, reconcile_task):
                task.cancel()
            await asyncio.gather(scan_task, manage_task, health_task, sub_task, price_alert_sync_task, stale_task, reconcile_task, return_exceptions=True)
            try:
                await self._publish_empty_price_alert_sync()
            except Exception as exc:
                self._logger.warning("[%s] Empty price_alert sync failed during shutdown: %s", self.config.ALPHA_ID, exc)
            self._logger.info("[%s] Shutting down", self.config.ALPHA_ID)

    async def _health_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                if self._ownership_healthy():
                    with open("/tmp/bot_health", "w") as health_file:
                        health_file.write(json_dumps({
                            "timestamp": time.time(),
                            "runtime_state": self.runtime_state,
                            "authoritative_revision": self._authoritative_revision,
                            "managed_position_ids": sorted(self._local_positions_by_id()),
                        }))
                else:
                    try:
                        os.remove("/tmp/bot_health")
                    except FileNotFoundError:
                        pass
            except Exception:
                pass
            await asyncio.sleep(10)

    def push_signal(self, signal_type: str, **kwargs) -> None:
        if signal_type in {"OPEN", "MODIFY", "CLOSE"}:
            position = None
            position_id = kwargs.get("position_id")
            for _symbol, candidate in getattr(self, "_open_positions", {}).items():
                if isinstance(candidate, dict) and candidate.get("position_id") == position_id:
                    position = candidate
                    break
            if position is not None:
                try:
                    metadata = json_loads(kwargs.get("metadata", "{}"))
                except Exception:
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata["strategy_runtime_version"] = 1
                metadata["strategy_runtime"] = self.serialize_position_runtime(position)
                kwargs["metadata"] = json_dumps(metadata)
        signal_push.push_signal(signal_type, self.config.ALPHA_ID, **kwargs)

    def register_columns(self, columns: list[dict]) -> None:
        self.push_signal("REGISTER_COLUMNS", columns=json_dumps(columns))

    def load_columns_config(self, path: str) -> None:
        import tomllib
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except FileNotFoundError:
            return
        columns = data.get("columns", [])
        if columns:
            self.register_columns(columns)

    def _replace_candle(self, sd: SymbolData, index: int, msg: dict) -> None:
        sd.open_list[index] = float(msg.get("open", 0.0))
        sd.high_list[index] = float(msg.get("high", 0.0))
        sd.low_list[index] = float(msg.get("low", 0.0))
        sd.price_list[index] = float(msg.get("close", 0.0))
        sd.volume_list[index] = float(msg.get("volume", 0.0))

    def _upsert_candle(self, sd: SymbolData, msg: dict, open_time: int) -> None:
        if sd.time_list:
            last = sd.time_list[-1]
            if open_time == last:
                self._replace_candle(sd, len(sd.time_list) - 1, msg)
                return
            if open_time > last:
                sd.time_list.append(open_time)
                sd.open_list.append(float(msg.get("open", 0.0)))
                sd.high_list.append(float(msg.get("high", 0.0)))
                sd.low_list.append(float(msg.get("low", 0.0)))
                sd.price_list.append(float(msg.get("close", 0.0)))
                sd.volume_list.append(float(msg.get("volume", 0.0)))
                return

            index = bisect_left(sd.time_list, open_time)
            if index < len(sd.time_list) and sd.time_list[index] == open_time:
                self._replace_candle(sd, index, msg)
                return
        else:
            index = 0

        sd.time_list.insert(index, open_time)
        sd.open_list.insert(index, float(msg.get("open", 0.0)))
        sd.high_list.insert(index, float(msg.get("high", 0.0)))
        sd.low_list.insert(index, float(msg.get("low", 0.0)))
        sd.price_list.insert(index, float(msg.get("close", 0.0)))
        sd.volume_list.insert(index, float(msg.get("volume", 0.0)))

    def _trim_symbol_data(self, sd: SymbolData) -> None:
        max_candles = getattr(self.config, "DATA_MAX_CANDLES", 1000)
        overflow = len(sd.time_list) - max_candles
        if overflow <= 0:
            return
        del sd.time_list[:overflow]
        del sd.open_list[:overflow]
        del sd.high_list[:overflow]
        del sd.low_list[:overflow]
        del sd.price_list[:overflow]
        del sd.volume_list[:overflow]
