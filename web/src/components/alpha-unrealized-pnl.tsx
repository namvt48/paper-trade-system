"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";
import type { Position } from "@/lib/types";
import { usePositionTicks, tickKey, type PositionTick } from "@/lib/use-position-ticks";

function firstPrice(...values: Array<number | null | undefined>) {
  return values.find((value): value is number => typeof value === "number" && Number.isFinite(value));
}

function livePnl(position: Position, tick: PositionTick | undefined) {
  if (!tick) return null;
  const currentPrice = position.side === "LONG"
    ? firstPrice(tick.bid, tick.last, tick.price)
    : firstPrice(tick.ask, tick.last, tick.price);
  if (currentPrice == null) return null;

  const direction = position.side === "LONG" ? 1 : -1;
  const grossPnl = (currentPrice - position.entry_price) * position.qty * direction;
  const estimatedFee = (position.entry_price + currentPrice) * position.qty * (position.fee_pct || 0);
  const pnl = grossPnl - estimatedFee;

  return pnl;
}

interface UnrealizedPnlContextValue {
  pnlByAlpha: Map<string, number | null>;
}

const UnrealizedPnlContext = createContext<UnrealizedPnlContextValue>({ pnlByAlpha: new Map() });

interface UnrealizedPnlProviderProps {
  hasOpenPositions: boolean;
  children: React.ReactNode;
}

/**
 * Opens ONE SSE stream to /api/position-ticks (no alpha_id = all open positions
 * across every alpha) and ONE fetch to /api/positions for the full position list.
 * Computes per-alpha unrealized PnL and shares it via context so each table row
 * can render its cell without opening its own connection.
 *
 * SSE lifecycle (including bfcache pagehide/pageshow handling) is managed by
 * usePositionTicks so connections never leak across browser back/forward.
 */
export function UnrealizedPnlProvider({ hasOpenPositions, children }: UnrealizedPnlProviderProps) {
  const [positions, setPositions] = useState<Position[]>([]);

  useEffect(() => {
    if (!hasOpenPositions) {
      setPositions([]);
      return;
    }
    let cancelled = false;
    fetch("/api/positions")
      .then((r) => r.json())
      .then((p: Position[]) => {
        if (!cancelled) setPositions(Array.isArray(p) ? p : []);
      })
      .catch(() => {
        // Keep cells silent on fetch failure; realized PnL column still renders.
      });
    return () => {
      cancelled = true;
    };
  }, [hasOpenPositions]);

  // No alpha_id = subscribe to ALL open positions across every alpha.
  // Only enable when we actually have positions to avoid opening a stream for nothing.
  const ticks = usePositionTicks(undefined, positions.length > 0);

  const pnlByAlpha = useMemo(() => {
    const map = new Map<string, number | null>();
    if (positions.length === 0) return map;
    for (const p of positions) {
      const tick = ticks[tickKey(p.exchange, p.symbol)];
      const pnl = livePnl(p, tick);
      const current = map.get(p.alpha_id);
      if (pnl == null) {
        if (current == null && !map.has(p.alpha_id)) map.set(p.alpha_id, null);
        continue;
      }
      if (current == null) {
        map.set(p.alpha_id, pnl);
      } else {
        map.set(p.alpha_id, current + pnl);
      }
    }
    return map;
  }, [positions, ticks]);

  return (
    <UnrealizedPnlContext.Provider value={{ pnlByAlpha }}>
      {children}
    </UnrealizedPnlContext.Provider>
  );
}

interface AlphaUnrealizedPnlProps {
  alphaId: string;
}

export function AlphaUnrealizedPnl({ alphaId }: AlphaUnrealizedPnlProps) {
  const { pnlByAlpha } = useContext(UnrealizedPnlContext);
  const value = pnlByAlpha.get(alphaId);

  if (value == null) {
    return <span className="text-slate-500">—</span>;
  }

  return (
    <span className={`font-mono font-semibold ${value >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
      {value >= 0 ? "+" : ""}{value.toFixed(4)}
    </span>
  );
}
