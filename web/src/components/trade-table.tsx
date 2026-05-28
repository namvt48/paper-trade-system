"use client";

import { useState } from "react";
import type { Trade } from "@/lib/types";

const PAGE_SIZE = 50;

interface TradeTableProps {
  trades: Trade[];
}

function fmtDate(iso: string) {
  if (!iso) return "-";
  return new Date(iso).toLocaleString("en-GB", {
    timeZone: "UTC",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function fmtPrice(v: number | null | undefined): string {
  if (v == null) return "—";
  return parseFloat(v.toPrecision(8)).toString();
}

function fmtDuration(h: number | null | undefined): string {
  if (h == null) return "—";
  if (h < 1) return `${Math.round(h * 60)}m`;
  if (h < 24) return `${h.toFixed(1)}h`;
  return `${(h / 24).toFixed(1)}d`;
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
    <div className="flex items-center gap-1 justify-end px-4 py-2.5 border-t border-slate-700/60 text-xs text-slate-400 select-none">
      <span className="mr-2 text-slate-500">{total} trades</span>
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

function downloadCsv(trades: Trade[]) {
  const headers = [
    "trade_id", "symbol", "side", "entry_price", "exit_price", "qty",
    "leverage", "tp", "sl", "pnl", "pnl_percent", "fee",
    "reason", "duration_hours", "opened_at", "closed_at",
  ];
  const escape = (v: unknown) => {
    const s = v == null ? "" : String(v);
    return s.includes(",") || s.includes('"') || s.includes("\n")
      ? `"${s.replace(/"/g, '""')}"`
      : s;
  };
  const rows = trades.map((t) =>
    [
      t.trade_id, t.symbol, t.side, t.entry_price, t.exit_price, t.qty,
      t.leverage, t.tp ?? "", t.sl ?? "", t.pnl, t.pnl_percent,
      (t as any).fee ?? "",
      t.reason, t.duration_hours, t.opened_at, t.closed_at,
    ].map(escape).join(",")
  );
  const csv = [headers.join(","), ...rows].join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `trades-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

export function TradeTable({ trades }: TradeTableProps) {
  const [page, setPage] = useState(1);
  const totalPages = Math.ceil(trades.length / PAGE_SIZE);
  const visible = trades.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  if (trades.length === 0) {
    return <div className="text-slate-600 text-center py-6 text-sm">No trades yet</div>;
  }

  return (
    <div className="rounded-xl border border-slate-700/60 overflow-hidden">
      <div className="flex justify-end px-4 py-2 border-b border-slate-700/60 bg-slate-800/60">
        <button
          onClick={() => downloadCsv(trades)}
          className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200 hover:bg-slate-700/60 px-3 py-1.5 rounded-lg transition-colors"
        >
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
          </svg>
          Download CSV ({trades.length})
        </button>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-slate-800/80 border-b border-slate-700/60 text-slate-400 text-xs uppercase tracking-wider">
              <th className="text-left py-3 px-3 whitespace-nowrap">Symbol</th>
              <th className="text-left py-3 px-3 whitespace-nowrap">Side</th>
              <th className="text-right py-3 px-3 whitespace-nowrap">Entry</th>
              <th className="text-right py-3 px-3 whitespace-nowrap">Exit</th>
              <th className="text-right py-3 px-3 whitespace-nowrap">Qty</th>
              <th className="text-right py-3 px-3 whitespace-nowrap">Leverage</th>
              <th className="text-right py-3 px-3 whitespace-nowrap">TP</th>
              <th className="text-right py-3 px-3 whitespace-nowrap">SL</th>
              <th className="text-right py-3 px-3 whitespace-nowrap">PnL</th>
              <th className="text-right py-3 px-3 whitespace-nowrap">PnL %</th>
              <th className="text-right py-3 px-3 whitespace-nowrap">Fee</th>
              <th className="text-left py-3 px-3 whitespace-nowrap">Reason</th>
              <th className="text-right py-3 px-3 whitespace-nowrap">Duration</th>
              <th className="text-left py-3 px-3 whitespace-nowrap">Opened (UTC)</th>
              <th className="text-left py-3 px-3 whitespace-nowrap">Closed (UTC)</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((t, i) => (
              <tr
                key={t.trade_id}
                className={`border-b border-slate-700/40 hover:bg-slate-700/30 transition-colors ${i % 2 === 0 ? "" : "bg-slate-800/30"}`}
              >
                <td className="py-2 px-3 font-mono text-slate-200 whitespace-nowrap">{t.symbol}</td>
                <td className={`py-2 px-3 font-semibold text-xs whitespace-nowrap ${t.side === "LONG" ? "text-emerald-400" : "text-rose-400"}`}>{t.side}</td>
                <td className="py-2 px-3 text-right font-mono text-slate-300 whitespace-nowrap">{fmtPrice(t.entry_price)}</td>
                <td className="py-2 px-3 text-right font-mono text-slate-300 whitespace-nowrap">{fmtPrice(t.exit_price)}</td>
                <td className="py-2 px-3 text-right font-mono text-slate-400 whitespace-nowrap text-xs">{parseFloat(t.qty.toPrecision(6)).toString()}</td>
                <td className="py-2 px-3 text-right font-mono text-indigo-300 whitespace-nowrap">{t.leverage}x</td>
                <td className="py-2 px-3 text-right font-mono text-emerald-400/80 whitespace-nowrap">{fmtPrice(t.tp)}</td>
                <td className="py-2 px-3 text-right font-mono text-rose-400/80 whitespace-nowrap">{fmtPrice(t.sl)}</td>
                <td className={`py-2 px-3 text-right font-mono font-semibold whitespace-nowrap ${t.pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                  {t.pnl >= 0 ? "+" : ""}{t.pnl.toFixed(4)}
                </td>
                <td className={`py-2 px-3 text-right font-mono whitespace-nowrap ${t.pnl_percent >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                  {t.pnl_percent >= 0 ? "+" : ""}{t.pnl_percent.toFixed(2)}%
                </td>
                <td className="py-2 px-3 text-right font-mono text-slate-500 text-xs whitespace-nowrap">
                  {(t as any).fee != null ? (t as any).fee.toFixed(4) : "—"}
                </td>
                <td className="py-2 px-3 text-slate-400 text-xs whitespace-nowrap">{t.reason}</td>
                <td className="py-2 px-3 text-right font-mono text-slate-400 text-xs whitespace-nowrap">{fmtDuration(t.duration_hours)}</td>
                <td className="py-2 px-3 text-slate-400 font-mono text-xs whitespace-nowrap">{fmtDate(t.opened_at)}</td>
                <td className="py-2 px-3 text-slate-400 font-mono text-xs whitespace-nowrap">{fmtDate(t.closed_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pagination page={page} total={trades.length} pageSize={PAGE_SIZE} onChange={(p) => { setPage(p); }} />
    </div>
  );
}
