"use client";

import { useMemo } from "react";
import type { AlphaStats, Position } from "@/lib/types";
import { usePositionTicks, tickKey, type PositionTick } from "@/lib/use-position-ticks";

interface StatsPanelProps {
  stats: AlphaStats;
  positions?: Position[];
  alphaId?: string;
}

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
  const capital = position.entry_price * position.qty;

  return {
    pnl,
    pnlPercent: capital ? pnl / capital * 100 : 0,
  };
}

export function StatsPanel({ stats, positions = [], alphaId }: StatsPanelProps) {
  const ticks = usePositionTicks(alphaId, Boolean(alphaId) && positions.length > 0);

  const totalUnrealizedPnl = useMemo(() => {
    if (positions.length === 0) return 0;
    let total = 0;
    let hasAnyTick = false;
    for (const p of positions) {
      const tick = ticks[tickKey(p.exchange, p.symbol)];
      const metrics = livePnl(p, tick);
      if (metrics) {
        total += metrics.pnl;
        hasAnyTick = true;
      }
    }
    return hasAnyTick ? total : 0;
  }, [positions, ticks]);

  const items = [
    { label: "Total Trades", value: stats.total_trades },
    { label: "Win / Loss", value: `${stats.win_trades} / ${stats.loss_trades}` },
    { label: "Winrate", value: `${stats.winrate.toFixed(1)}%` },
    { label: "Total PnL", value: stats.total_pnl.toFixed(4), highlight: stats.total_pnl > 0 },
    {
      label: "Unrealized PnL",
      value: totalUnrealizedPnl.toFixed(4),
      highlight: totalUnrealizedPnl > 0 ? true : totalUnrealizedPnl < 0 ? false : undefined,
    },
    { label: "Avg PnL", value: stats.avg_pnl.toFixed(4), highlight: stats.avg_pnl > 0 },
    { label: "Avg Win", value: stats.avg_win?.toFixed(4) || "-" },
    { label: "Avg Loss", value: stats.avg_loss?.toFixed(4) || "-" },
    { label: "Max Drawdown", value: stats.max_drawdown?.toFixed(4) || "0" },
    { label: "Sharpe Ratio", value: stats.sharpe_ratio?.toFixed(2) || "0" },
    { label: "Consec. Wins", value: stats.consecutive_wins },
    { label: "Consec. Losses", value: stats.consecutive_losses },
  ];

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
      {items.map((item) => (
        <div key={item.label} className="bg-slate-800/60 border border-slate-700/60 rounded-xl p-3">
          <div className="text-slate-400 text-xs mb-1 uppercase tracking-wide">{item.label}</div>
          <div className={`font-mono text-lg font-semibold ${
            item.highlight === true
              ? "text-emerald-400"
              : item.highlight === false
              ? "text-rose-400"
              : "text-slate-200"
          }`}>
            {item.value}
          </div>
        </div>
      ))}
    </div>
  );
}

