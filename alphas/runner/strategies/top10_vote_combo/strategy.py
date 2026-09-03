"""Top-10 vote-combination cross-sectional alpha (1d candles, 54-symbol universe).

Combination of 10 independent single-factor alphas. Each alpha z-scores one
transformed factor across the fixed whitelist universe (the 3 curated pools,
already collapsed into ONE 54-symbol whitelist -- pools are NOT re-introduced
here) and casts a +/-1 vote per symbol. Symbols with a non-zero net vote are
traded; sizing is proportional to |vote_sum|, normalized to gross 1 over the
traded symbols.

Factor math is a VERBATIM port of ``docs/run_top10.py`` (``compute_factors``,
``TRANSFORMS``, cross-section z-score + 3-sigma winsor from ``compute_signal``).
Differences from the research script (locked with user):

- Data source is the runner candle cache (per-symbol ``ctx.cache`` snapshots
  for all universe symbols at the last CLOSED 1d bar), not Binance fetches.
- ``quote_volume`` is the PROXY ``close * volume`` (same convention as
  cross_alpha) -- the runner cache does not carry kline quote volume.
- Universe is whitelist-only with NO liquidity mask (all pool symbols liquid).
- Residual factor votes are SKIPPED entirely when BTCUSDT is missing from the
  panel (research script falls back to the universe-mean market return).
- Tie (vote_sum == 0) -> abstain: no position for that symbol.
- NO TP/SL/stops: signal-driven daily basket replacement only.

Rebalance semantics (mirrors ``cross_sectional._apply_selection``):

- On every newly completed 1d candle (coverage gate >= ``scan_min_symbol_coverage``,
  BYPASSED while holding open positions -- the 2026-08-21 incident fix), CLOSE
  EVERY held position with reason ``REBALANCE``, then OPEN the new basket
  (CLOSE-all-then-OPEN per rebalance, including symbols retained in the basket).
- Candle semantics follow ``bollinger_meanrev_ls``: the candle cache ends with
  the still-forming candle, so factors run on the closed prefix (panel minus the
  row at the forming candle's open timestamp) and fills price at the OPEN of the
  just-started candle (the moment the signal candle closed).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from runner.strategy.base import Strategy

logger = logging.getLogger(__name__)

TF = "1d"
D1_MS = 24 * 60 * 60 * 1000

DEFAULT_WARMUP_BARS = 260  # z120 needs 120 + chg60 needs 60 + buffer
DEFAULT_CAPITAL = 10_000.0
DEFAULT_LEVERAGE = 1.0
DEFAULT_FEE_PCT = 0.0007
DEFAULT_SCAN_MIN_SYMBOL_COVERAGE = 0.9

# Minimum CLOSED-bar rows required before voting is meaningful: chg60 at the
# last row references factor row -61, so at least 61 closed rows are needed.
MIN_CLOSED_BARS = 61

# Verbatim from docs/run_top10.py -- the 10 single-factor alphas. The
# ``factor`` keys match ``compute_factors`` output keys.
TOP10_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "lower_shadow|chg60|-",
        "factor": "ohlc_vol:lower_shadow",
        "transform": "chg60",
        "sign": -1,
    },
    {
        "name": "lower_shadow|chg20|-",
        "factor": "ohlc_vol:lower_shadow",
        "transform": "chg20",
        "sign": -1,
    },
    {
        "name": "lower_shadow|z60|-",
        "factor": "ohlc_vol:lower_shadow",
        "transform": "z60",
        "sign": -1,
    },
    {
        "name": "lower_shadow|z120|-",
        "factor": "ohlc_vol:lower_shadow",
        "transform": "z120",
        "sign": -1,
    },
    {
        "name": "body|chg60|-",
        "factor": "ohlc_vol:body",
        "transform": "chg60",
        "sign": -1,
    },
    {"name": "clv|chg5|-", "factor": "ohlc_vol:clv", "transform": "chg5", "sign": -1},
    {
        "name": "upper_shadow|z120|-",
        "factor": "ohlc_vol:upper_shadow",
        "transform": "z120",
        "sign": -1,
    },
    {"name": "clv|chg60|-", "factor": "ohlc_vol:clv", "transform": "chg60", "sign": -1},
    {
        "name": "spread_ar|z120|+",
        "factor": "liquidity:spread_ar",
        "transform": "z120",
        "sign": 1,
    },
    {
        "name": "residual_returns|chg60|-",
        "factor": "residual:residual_returns",
        "transform": "chg60",
        "sign": -1,
    },
)

# CandleSnapshot attribute -> panel field (no quote volume in the runner cache).
_SNAPSHOT_FIELDS = (
    ("open", "opens"),
    ("high", "highs"),
    ("low", "lows"),
    ("close", "closes"),
    ("volume", "volumes"),
)


# ==================== FACTORS (verbatim port of docs/run_top10.py) ====================


def compute_factors(P: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Compute all 6 factor panels. VERBATIM port of docs/run_top10.py.

    ``P`` holds DataFrames keyed by symbol (rows = bar timestamps):
    ``open / high / low / close / volume / quote_volume``.
    ``quote_volume`` is the ``close * volume`` proxy (see module docstring).
    """
    O, H, L, C, V, QV = (
        P["open"],
        P["high"],
        P["low"],
        P["close"],
        P["volume"],
        P["quote_volume"],
    )
    ret = C.pct_change()
    hl = (H - L).replace(0, np.nan)
    oc_max = O.where(O > C, C)
    oc_min = O.where(O < C, C)

    F = {}
    F["ohlc_vol:lower_shadow"] = (oc_min - L) / hl
    F["ohlc_vol:upper_shadow"] = (H - oc_max) / hl
    F["ohlc_vol:body"] = (C - O).abs() / hl
    F["ohlc_vol:clv"] = ((C - L) - (H - C)) / hl
    F["liquidity:spread_ar"] = (
        (ret.abs() / QV.replace(0, np.nan)).rolling(20, min_periods=10).mean()
    )

    btc = ret.get("BTCUSDT", ret.mean(axis=1))
    cov = ret.rolling(60, min_periods=30).cov(btc)
    var = btc.rolling(60, min_periods=30).var()
    beta = cov.div(var, axis=0)
    F["residual:residual_returns"] = ret.sub(beta.mul(btc, axis=0))
    return F


TRANSFORMS: dict[str, Any] = {
    "chg5": lambda F: F - F.shift(5),
    "chg20": lambda F: F - F.shift(20),
    "chg60": lambda F: F - F.shift(60),
    "z20": lambda F: (
        (F - F.rolling(20, min_periods=10).mean())
        / F.rolling(20, min_periods=10).std().replace(0, np.nan)
    ),
    "z60": lambda F: (
        (F - F.rolling(60, min_periods=30).mean())
        / F.rolling(60, min_periods=30).std().replace(0, np.nan)
    ),
    "z120": lambda F: (
        (F - F.rolling(120, min_periods=60).mean())
        / F.rolling(120, min_periods=60).std().replace(0, np.nan)
    ),
}


# ==================== VOTING ====================


@dataclass(frozen=True)
class VoteSelection:
    """Per-symbol voting outcome.

    ``weights``: SIGNED sizing weights over TRADED symbols only (|vote_sum|
    normalized so sum(|w|) == 1). LONG when w > 0, SHORT when w < 0.
    ``vote_sums`` / ``up_votes`` / ``down_votes`` cover every universe symbol.
    ``scores``: signed conviction vote_sum / n_specs in [-1, 1] (traded only).
    """

    weights: dict[str, float]
    vote_sums: dict[str, int]
    up_votes: dict[str, int]
    down_votes: dict[str, int]
    scores: dict[str, float]


@dataclass(frozen=True)
class SelectionPayload:
    selection: VoteSelection
    entry_prices: dict[str, float]
    exit_prices: dict[str, float]


def cross_section_votes(raw_row: pd.Series, sign: int) -> pd.Series:
    """One alpha's +/-1 votes over a single latest cross-section.

    ``cs_z = (x - mean) / std`` across the universe (skipna, ``std(ddof=1)``;
    std == 0 or all-NaN cross-section -> all votes 0, i.e. this alpha abstains).
    ``cs_z`` is winsorized at mean +/- 3 sigma (the clip step of
    docs/run_top10.py ``compute_signal``), then ``vote = sign(cs_z * spec_sign)``.
    Symbols with NaN factor values abstain (vote 0).
    """
    x = pd.to_numeric(raw_row, errors="coerce").astype(float)
    mean = x.mean()
    std = x.std(ddof=1)
    if not np.isfinite(mean) or not np.isfinite(std) or std <= 0.0:
        return pd.Series(0.0, index=raw_row.index)
    cs_z = ((x - mean) / std).fillna(0.0)
    w_mean, w_std = cs_z.mean(), cs_z.std(ddof=1)
    if np.isfinite(w_mean) and np.isfinite(w_std) and w_std > 0.0:
        cs_z = cs_z.clip(lower=w_mean - 3.0 * w_std, upper=w_mean + 3.0 * w_std)
    votes = np.sign(cs_z * int(sign)).fillna(0.0)
    return votes


def aggregate_votes(vote_matrix: pd.DataFrame) -> VoteSelection:
    """Aggregate per-alpha votes into directions and sizing weights.

    ``vote_matrix``: rows = alpha specs, columns = symbols, cells in {-1, 0, +1}.
    direction: vote_sum > 0 -> LONG, < 0 -> SHORT, == 0 -> NO TRADE (tie abstains).
    strength = |vote_sum|; weights = strength normalized so sum(|w|) == 1 over
    traded symbols.
    """
    if vote_matrix.empty:
        return VoteSelection({}, {}, {}, {}, {})
    filled = vote_matrix.fillna(0.0)
    vote_sum = filled.sum(axis=0)
    up = (filled > 0).sum(axis=0)
    down = (filled < 0).sum(axis=0)
    n_specs = len(filled)
    traded = vote_sum[vote_sum != 0]
    gross = float(traded.abs().sum())
    weights: dict[str, float] = (
        {sym: float(value / gross) for sym, value in traded.items()}
        if gross > 0
        else {}
    )
    return VoteSelection(
        weights=weights,
        vote_sums={sym: int(round(float(value))) for sym, value in vote_sum.items()},
        up_votes={sym: int(value) for sym, value in up.items()},
        down_votes={sym: int(value) for sym, value in down.items()},
        scores={sym: float(vote_sum[sym]) / n_specs for sym in weights},
    )


def build_vote_matrix(
    P: dict[str, pd.DataFrame], specs: tuple[dict[str, Any], ...] = TOP10_SPECS
) -> pd.DataFrame:
    """Rows = alpha spec votes over the latest cross-section, columns = symbols.

    The residual factor's votes are skipped entirely when BTCUSDT is missing
    from the panel (locked rule; see module docstring).
    """
    factors = compute_factors(P)
    has_btc = "BTCUSDT" in P["close"].columns
    rows: dict[str, pd.Series] = {}
    for spec in specs:
        if spec["factor"] == "residual:residual_returns" and not has_btc:
            continue
        raw = TRANSFORMS[spec["transform"]](factors[spec["factor"]])
        rows[spec["name"]] = cross_section_votes(raw.iloc[-1], int(spec["sign"]))
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).T


def select_top10_basket(
    P: dict[str, pd.DataFrame], specs: tuple[dict[str, Any], ...] = TOP10_SPECS
) -> VoteSelection:
    return aggregate_votes(build_vote_matrix(P, specs))


# ==================== STRATEGY ====================


class Top10VoteComboRunnerStrategy(Strategy):
    """10 factor alphas vote daily on a fixed whitelist universe; basket replacement."""

    def __init__(self, alpha_id: str, version: str, params: dict, ctx) -> None:
        super().__init__(alpha_id, version, params, ctx)
        self._alphas_root = Path(__file__).resolve().parents[3]
        self.exchange = str(params.get("exchange", "binance"))
        self.capital = float(params.get("capital", DEFAULT_CAPITAL))
        self.leverage = float(params.get("leverage", DEFAULT_LEVERAGE))
        self.fee_pct = float(params.get("fee_pct", DEFAULT_FEE_PCT))
        self.warmup_bars = int(params.get("warmup_bars", DEFAULT_WARMUP_BARS))
        self.retain_bars = int(params.get("retain_bars", self.warmup_bars))
        self.scan_min_symbol_coverage = float(
            params.get("scan_min_symbol_coverage", DEFAULT_SCAN_MIN_SYMBOL_COVERAGE)
        )
        self._symbols = self._load_universe()
        self._symbol_set = set(self._symbols)
        self._last_processed_candle = 0
        self._open_positions: dict[str, dict[str, Any]] = (
            self.reconcile_open_positions()
        )
        self._sync_price_alerts()

    # ------------------------------------------------------------------ wiring

    @classmethod
    def get_required_channels(cls, params: dict) -> list[str]:
        return [f"kline:{TF}"]

    def get_required_channels_instance(self) -> list[str]:
        # Mirror cross_sectional: also subscribe to MDS's live symbol universe
        # broadcast for the instance (kept out of the classmethod list, which
        # feeds kline tf derivation in main.py).
        channels = list(self.__class__.get_required_channels(self.params))
        channels.append(f"symbols:{self.exchange}")
        return channels

    def get_warmup_symbols(self) -> list[str]:
        return list(self._symbols)

    def get_warmup_tfs(self) -> list[str]:
        return [TF]

    def get_warmup_bars(self, tf: str) -> int:
        return self.warmup_bars

    def get_retain_bars(self, tf: str) -> int:
        return max(self.warmup_bars, self.retain_bars)

    # ---------------------------------------------------------------- universe

    def _resolve_path(self, value: str) -> Path:
        """Resolve like cross_sectional._resolve_path: cwd first, then alphas/ root."""
        path = Path(value)
        if path.is_absolute():
            return path
        if path.exists():
            return path
        return self._alphas_root / path

    def _load_universe(self) -> list[str]:
        """Whitelist-only universe (newline-delimited text). No liquidity mask."""
        value = self.params.get("whitelist_file")
        if not value:
            raise ValueError(f"{self.alpha_id} missing required params.whitelist_file")
        path = self._resolve_path(str(value))
        if not path.exists():
            raise FileNotFoundError(path)
        symbols = [
            line.strip().upper()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not symbols:
            raise ValueError(f"{self.alpha_id} whitelist {path} has no symbols")
        return symbols

    # ------------------------------------------------------------- scan gating

    def should_scan_after_event(
        self, kind: str, symbol: str | None = None, tf: str | None = None
    ) -> bool:
        if kind != "kline" or tf != TF or not symbol:
            return False
        if symbol not in self._symbol_set:
            return False
        candle_open_ms = self.ctx.cache.get_latest_timestamp(symbol, TF)
        if candle_open_ms is None or candle_open_ms <= self._last_processed_candle:
            return False
        # Coverage gate bypass while holding positions (2026-08-21 incident
        # fix, mirrors cross_sectional): held symbols without fresh data must
        # still be CLOSEd at rebalance instead of freezing the basket open.
        if self._open_positions:
            return True
        if self._candle_coverage(candle_open_ms) < self.scan_min_symbol_coverage:
            return False
        return True

    def _candle_coverage(self, candle_open_ms: int) -> float:
        if not self._symbols:
            return 0.0
        loaded = 0
        for symbol in self._symbols:
            ts = self.ctx.cache.get_latest_timestamp(symbol, TF)
            if ts is not None and int(ts) >= int(candle_open_ms):
                loaded += 1
        return loaded / len(self._symbols)

    def _latest_cached_timestamp(self) -> int:
        latest = 0
        for symbol in self._symbols:
            ts = self.ctx.cache.get_latest_timestamp(symbol, TF)
            if ts is not None and int(ts) > latest:
                latest = int(ts)
        return latest

    # -------------------------------------------------------------------- scan

    async def scan(self) -> None:
        # Readiness normally gates the whole scan; held positions are allowed
        # through so missing-data symbols get closed at rebalance (mirrors
        # cross_sectional.scan's 2026-08-21 exception).
        if not self.ctx.state.ready and not self._open_positions:
            return
        latest = self._latest_cached_timestamp()
        if latest <= self._last_processed_candle:
            return
        snapshots = self._collect_snapshots()
        payload = await asyncio.to_thread(self._compute_selection, snapshots, latest)
        await self._apply_selection(payload, latest)
        self._last_processed_candle = latest

    def _collect_snapshots(self) -> dict[str, Any]:
        bars = self.get_warmup_bars(TF)
        snapshots: dict[str, Any] = {}
        for symbol in self._symbols:
            snap = self.ctx.cache.snapshot(symbol, TF, bars)
            if not snap.times:
                continue
            snapshots[symbol] = snap
        return snapshots

    def _compute_selection(
        self, snapshots: dict[str, Any], latest: int
    ) -> SelectionPayload | None:
        """CPU-bound pandas work (run via asyncio.to_thread).

        Builds the union-indexed panel across all universe symbols, drops the
        row at ``latest`` (the still-forming candle -- factors run on CLOSED
        bars only, bollinger_meanrev_ls ``closes[:-1]`` semantics), computes
        the vote basket, and resolves fill prices: entry = open of the
        just-started candle (next-bar-open fill), fallback last closed close;
        exit reference = closed-panel close row.
        """
        if not snapshots:
            return None
        frames: dict[str, dict[str, pd.Series]] = {}
        for symbol, snap in snapshots.items():
            index = list(snap.times)
            for field, attr in _SNAPSHOT_FIELDS:
                frames.setdefault(field, {})[symbol] = pd.Series(
                    list(getattr(snap, attr)), index=index, dtype="float64"
                )
        panels: dict[str, pd.DataFrame] = {}
        for field, per_symbol in frames.items():
            frame = pd.DataFrame(per_symbol)
            panels[field] = frame[frame.index < latest]  # drop forming candle row
        closed = panels["close"]
        if closed.shape[0] < MIN_CLOSED_BARS or closed.dropna(how="all").empty:
            return None
        # quote_volume PROXY = close * volume (documented in module docstring;
        # same convention as cross_alpha).
        panels["quote_volume"] = panels["close"] * panels["volume"]
        selection = select_top10_basket(panels)

        last_close_row = closed.ffill().iloc[-1]
        exit_prices = {
            str(sym): float(value)
            for sym, value in last_close_row.dropna().items()
            if np.isfinite(value) and float(value) > 0
        }
        entry_prices: dict[str, float] = {}
        for symbol, snap in snapshots.items():
            if int(snap.times[-1]) == int(latest) and float(snap.opens[-1]) > 0:
                entry_prices[symbol] = float(snap.opens[-1])
        for symbol in selection.weights:
            if symbol not in entry_prices:
                fallback = exit_prices.get(symbol)
                if fallback is not None:
                    entry_prices[symbol] = fallback
        return SelectionPayload(
            selection=selection, entry_prices=entry_prices, exit_prices=exit_prices
        )

    # ----------------------------------------------------------------- signals

    async def _apply_selection(
        self, payload: SelectionPayload | None, candle_open_ms: int
    ) -> None:
        if payload is None:
            # Insufficient data on this bar: emit nothing (positions untouched).
            return
        selection = payload.selection
        logger.info(
            "[%s] _apply_selection at %d: traded=%d long=%d short=%d",
            self.alpha_id,
            candle_open_ms,
            len(selection.weights),
            sum(1 for w in selection.weights.values() if w > 0),
            sum(1 for w in selection.weights.values() if w < 0),
            extra={"alpha_id": self.alpha_id},
        )
        # CLOSE-all-then-OPEN per rebalance (mirrors cross_sectional):
        # every held position is closed with reason REBALANCE, including
        # symbols retained in the new basket (they get re-opened below).
        for symbol, pos in list(self._open_positions.items()):
            await self.ctx.emit_signal(
                "CLOSE",
                symbol=symbol,
                tf=TF,
                position_id=str(pos.get("position_id", "")),
                exit_price=self._exit_price(symbol, pos, payload.exit_prices),
                reason="REBALANCE",
                signal_candle_open_ms=candle_open_ms,
            )
        self._open_positions.clear()

        if not self.ctx.can_open_trades():
            logger.info(
                "[%s] _apply_selection: OPENs gated (can_open_trades=False)",
                self.alpha_id,
                extra={"alpha_id": self.alpha_id},
            )
            self.ctx.clear_positions()
            self._sync_price_alerts()
            return

        for symbol, weight in selection.weights.items():
            price = payload.entry_prices.get(symbol)
            if not price or float(price) <= 0:
                logger.warning(
                    "[%s] OPEN skipped for %s: no fill price available",
                    self.alpha_id,
                    symbol,
                    extra={"alpha_id": self.alpha_id},
                )
                continue
            price = float(price)
            side = "LONG" if weight > 0 else "SHORT"
            notional = self.capital * abs(weight) * self.leverage
            qty = notional / price
            position_id = str(uuid.uuid4())
            self._open_positions[symbol] = {
                "position_id": position_id,
                "symbol": symbol,
                "side": side,
                "entry": price,
                "qty": qty,
                "weight": float(weight),
                "leverage": self.leverage,
                "entry_candle_open_ms": candle_open_ms,
            }
            metadata = {
                "strategy_type": "top10_vote_combo",
                "vote_sum": int(selection.vote_sums.get(symbol, 0)),
                "strength": int(abs(selection.vote_sums.get(symbol, 0))),
                "up_votes": int(selection.up_votes.get(symbol, 0)),
                "down_votes": int(selection.down_votes.get(symbol, 0)),
                "score": float(selection.scores.get(symbol, 0.0)),
                "weight": float(weight),
            }
            await self.ctx.emit_signal(
                "OPEN",
                symbol=symbol,
                side=side,
                tf=TF,
                entry=price,
                qty=qty,
                leverage=1,
                position_id=position_id,
                exchange=self.exchange,
                fee_pct=self.fee_pct,
                metadata=json.dumps(metadata, sort_keys=True),
                signal_candle_open_ms=candle_open_ms,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        if self._open_positions:
            self.ctx.save_positions(self._open_positions)
        else:
            self.ctx.clear_positions()
        self._sync_price_alerts()

    def _exit_price(
        self, symbol: str, pos: dict[str, Any], exit_prices: dict[str, float]
    ) -> float:
        """CLOSE fill reference ladder (mirrors cross_sectional._closest_exit_price):
        closed-panel close (ffilled) -> last cached close -> entry price."""
        candidate = exit_prices.get(symbol)
        if candidate is not None and candidate > 0:
            return float(candidate)
        cached = self._last_cached_close(symbol)
        if cached is not None:
            return cached
        return float(pos.get("entry") or pos.get("entry_price") or 0.0)

    def _last_cached_close(self, symbol: str) -> float | None:
        try:
            closes = self.ctx.cache.get_closes(symbol, TF, 1)
        except Exception:
            return None
        if not closes:
            return None
        try:
            value = float(closes[-1])
        except (TypeError, ValueError, IndexError):
            return None
        return value if value > 0 else None

    # ------------------------------------------------------ position lifecycle

    async def manage_positions(self) -> None:
        self._sync_price_alerts()

    def _sync_price_alerts(self) -> None:
        """Register held symbols with MDS for price_alert ticks (mirrors
        cross_sectional._sync_price_alerts)."""
        if self.ctx.price_alerts is None:
            return
        try:
            self.ctx.price_alerts.sync(set(self._open_positions.keys()))
        except Exception:
            logger.exception(
                "[%s] _sync_price_alerts failed",
                self.alpha_id,
                extra={"alpha_id": self.alpha_id},
            )
