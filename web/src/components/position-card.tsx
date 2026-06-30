"use client";

import { useMemo, useState } from "react";
import type { Position } from "@/lib/types";
import { usePositionTicks, tickKey, type PositionTick } from "@/lib/use-position-ticks";

const PAGE_SIZE = 50;

type SortColumn = "side" | "pnl";
type SortDirection = "asc" | "desc";
interface SortState {
  column: SortColumn;
  direction: SortDirection;
}

interface PositionCardProps {
  alphaId: string;
  positions: Position[];
}

function fmtTime(iso: string) {
  if (!iso) return "-";
  return new Date(iso).toLocaleString("en-GB", {
    timeZone: "UTC",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }) + " UTC";
}

function elapsed(iso: string) {
  if (!iso) return "";
  const ms = Date.now() - new Date(iso).getTime();
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function fmtPrice(v: number | null | undefined): string {
  if (v == null) return "—";
  return parseFloat(v.toPrecision(8)).toString();
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

function Pagination({
  page,
  total,
  pageSize,
  onChange,
}: {
  page: number;
  total: number;
  pageSize: number;
  onChange: (p: number) => void;
}) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  if (totalPages <= 1) return null;
  const pages: (number | "…")[] = [];
  if (totalPages <= 7) {
    for (let i = 1; i <= totalPages; i++) pages.push(i);
  } else {
    pages.push(1);
    if (page > 3) pages.push("…");
    for (let i = Math.max(2, page - 1); i <= Math.min(totalPages - 1, page + 1); i++) pages.push(i);
    if (page < totalPages - 2) pages.push("…");
    pages.push(totalPages);
  }
  return (
    <div className="flex items-center gap-1 justify-end px-3 py-2.5 sm:px-4 border-t border-slate-700/60 text-xs text-slate-400 select-none">
      <span className="mr-2 text-slate-500 hidden sm:inline">{total} positions</span>
      <button
        onClick={() => onChange(page - 1)}
        disabled={page === 1}
        className="px-2 py-1 rounded hover:bg-slate-700/60 disabled:opacity-30 disabled:cursor-not-allowed"
      >
        ‹
      </button>
      {pages.map((p, i) =>
        p === "…" ? (
          <span key={`ellipsis-${i}`} className="px-1">…</span>
        ) : (
          <button
            key={p}
            onClick={() => onChange(p as number)}
            className={`w-7 py-1 rounded ${p === page ? "bg-indigo-600/70 text-white" : "hover:bg-slate-700/60"}`}
          >
            {p}
          </button>
        )
      )}
      <button
        onClick={() => onChange(page + 1)}
        disabled={page === totalPages}
        className="px-2 py-1 rounded hover:bg-slate-700/60 disabled:opacity-30 disabled:cursor-not-allowed"
      >
        ›
      </button>
    </div>
  );
}

export function PositionCard({ alphaId, positions }: PositionCardProps) {
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState<SortState | null>(null);
  const ticks = usePositionTicks(alphaId);

  const sortedPositions = useMemo(() => {
    if (!sort) return positions;
    const sorted = [...positions];
    if (sort.column === "side") {
      sorted.sort((a, b) => {
        const cmp = a.side.localeCompare(b.side);
        return sort.direction === "asc" ? cmp : -cmp;
      });
    } else {
      sorted.sort((a, b) => {
        const pnlA = livePnl(a, ticks[tickKey(a.exchange, a.symbol)]);
        const pnlB = livePnl(b, ticks[tickKey(b.exchange, b.symbol)]);
        if (pnlA == null && pnlB == null) return 0;
        if (pnlA == null) return 1;
        if (pnlB == null) return -1;
        return sort.direction === "asc" ? pnlA.pnl - pnlB.pnl : pnlB.pnl - pnlA.pnl;
      });
    }
    return sorted;
  }, [positions, sort, ticks]);

  const visible = sortedPositions.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const toggleSort = (column: SortColumn) => {
    setSort((current) => {
      if (current?.column === column) {
        return { column, direction: current.direction === "asc" ? "desc" : "asc" };
      }
      return { column, direction: column === "pnl" ? "desc" : "asc" };
    });
    setPage(1);
  };

  if (positions.length === 0) {
    return <div className="text-slate-500 text-center py-6 text-sm">No open positions</div>;
  }

  return (
    <div className="rounded-xl border border-slate-700/60 overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm min-w-[900px]">
          <thead>
            <tr className="bg-slate-800/80 border-b border-slate-700/60 text-slate-400 text-xs uppercase tracking-wider">
              <th className="text-left py-3 px-4 whitespace-nowrap">Symbol</th>
              <th className="text-left py-3 px-4 whitespace-nowrap">
                <button
                  onClick={() => toggleSort("side")}
                  className="inline-flex items-center gap-1 text-left hover:text-slate-200 transition-colors select-none"
                >
                  Side
                  {sort?.column === "side" && (
                    <span className="text-indigo-400">{sort.direction === "asc" ? "\u25B2" : "\u25BC"}</span>
                  )}
                </button>
              </th>
              <th className="text-right py-3 px-4 whitespace-nowrap">Entry</th>
              <th className="text-right py-3 px-4 whitespace-nowrap">Qty</th>
              <th className="text-right py-3 px-4 whitespace-nowrap">TP</th>
              <th className="text-right py-3 px-4 whitespace-nowrap">SL</th>
              <th className="text-right py-3 px-4 whitespace-nowrap">Leverage</th>
              <th className="text-right py-3 px-4 whitespace-nowrap">
                <button
                  onClick={() => toggleSort("pnl")}
                  className="inline-flex items-center gap-1 text-right hover:text-slate-200 transition-colors select-none"
                >
                  Live PnL
                  {sort?.column === "pnl" && (
                    <span className="text-indigo-400">{sort.direction === "asc" ? "\u25B2" : "\u25BC"}</span>
                  )}
                </button>
              </th>
              <th className="text-left py-3 px-4 whitespace-nowrap">Opened (UTC)</th>
              <th className="text-right py-3 px-4 whitespace-nowrap">Duration</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((p, i) => {
              const metrics = livePnl(p, ticks[tickKey(p.exchange, p.symbol)]);
              return (
                <tr
                  key={p.position_id}
                  className={`border-b border-slate-700/40 hover:bg-slate-700/30 transition-colors ${i % 2 === 0 ? "" : "bg-slate-800/30"}`}
                >
                  <td className="py-2.5 px-4 font-mono font-bold text-white whitespace-nowrap">{p.symbol}</td>
                  <td className={`py-2.5 px-4 font-semibold text-xs whitespace-nowrap ${p.side === "LONG" ? "text-emerald-400" : "text-rose-400"}`}>
                    {p.side}
                  </td>
                  <td className="py-2.5 px-4 text-right font-mono text-slate-200 whitespace-nowrap">{fmtPrice(p.entry_price)}</td>
                  <td className="py-2.5 px-4 text-right font-mono text-slate-400 text-xs whitespace-nowrap">{parseFloat(p.qty.toPrecision(6)).toString()}</td>
                  <td className="py-2.5 px-4 text-right font-mono text-emerald-400 whitespace-nowrap">{fmtPrice(p.tp)}</td>
                  <td className="py-2.5 px-4 text-right font-mono text-rose-400 whitespace-nowrap">{fmtPrice(p.sl)}</td>
                  <td className="py-2.5 px-4 text-right font-mono text-indigo-300 whitespace-nowrap">{p.leverage}x</td>
                  <td className={`py-2.5 px-4 text-right font-mono font-semibold whitespace-nowrap ${
                    metrics == null ? "text-slate-500" : metrics.pnl >= 0 ? "text-emerald-400" : "text-rose-400"
                  }`}>
                    {metrics == null ? "—" : (
                      <>
                        <div>{metrics.pnl >= 0 ? "+" : ""}{metrics.pnl.toFixed(4)}</div>
                        <div className="text-[10px] opacity-75">{metrics.pnlPercent >= 0 ? "+" : ""}{metrics.pnlPercent.toFixed(2)}%</div>
                      </>
                    )}
                  </td>
                  <td className="py-2.5 px-4 font-mono text-slate-400 text-xs whitespace-nowrap">{fmtTime(p.opened_at)}</td>
                  <td className="py-2.5 px-4 text-right font-mono text-indigo-400 text-xs whitespace-nowrap">{elapsed(p.opened_at)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <Pagination page={page} total={positions.length} pageSize={PAGE_SIZE} onChange={setPage} />
    </div>
  );
}
