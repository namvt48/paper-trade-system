import { getAlpha, getAlphaStats, getTrades, getEquityCurve, getOpenPositions, getAlphaConfig, getAlphaColumns, TRADE_HISTORY_LIMIT } from "@/lib/db";
import { EquityChart } from "@/components/equity-chart";
import { TradeTable } from "@/components/trade-table";
import { PositionCard } from "@/components/position-card";
import { StatsPanel } from "@/components/stats-panel";
import { notFound } from "next/navigation";

export const dynamic = "force-dynamic";

function formatConfigValue(value: unknown) {
  if (value === null || value === undefined || value === "") return "-";
  return String(value);
}

function fmtDate(iso: string) {
  if (!iso) return "-";
  return new Date(iso).toLocaleString("en-GB", {
    timeZone: "UTC",
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }) + " UTC";
}

export default async function AlphaDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const alpha = getAlpha(id);
  if (!alpha) notFound();

  const stats = getAlphaStats(id);
  const positions = getOpenPositions(id);
  const trades = getTrades(id, TRADE_HISTORY_LIMIT);
  const equity = getEquityCurve(id);
  const config = getAlphaConfig(id);
  const columnSpecs = getAlphaColumns(id);
  const configEntries = Object.entries(config);

  return (
    <div className="space-y-8">
      <div className="flex items-start justify-between">
        <div>
          <a href="/" className="text-slate-500 hover:text-slate-300 text-sm transition-colors">&larr; Dashboard</a>
          <h1 className="text-2xl font-bold text-white mt-1">{alpha.display_name}</h1>
          <div className="flex items-center gap-3 mt-2">
            <span className={`inline-flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full border ${
              alpha.status === "active"
                ? "bg-emerald-900/50 border-emerald-600/50 text-emerald-300"
                : "bg-slate-700/50 border-slate-600/50 text-slate-300"
            }`}>
              <span className={`w-1.5 h-1.5 rounded-full ${alpha.status === "active" ? "bg-emerald-400 animate-pulse" : "bg-slate-500"}`} />
              {alpha.status || "unknown"}
            </span>
            <span className="text-slate-500 text-xs font-mono">
              Started: <span className="text-slate-400">{fmtDate(alpha.created_at)}</span>
            </span>
          </div>
        </div>
        <div className="text-right">
          <p className="text-slate-500 text-xs mb-0.5">Total PnL</p>
          <div className={`text-2xl font-mono font-bold ${stats.total_pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
            {stats.total_pnl >= 0 ? "+" : ""}{stats.total_pnl.toFixed(4)}
          </div>
        </div>
      </div>

      <div>
        <SectionHeader>Statistics</SectionHeader>
        <StatsPanel stats={stats} />
      </div>

      <div>
        <SectionHeader>Config</SectionHeader>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {configEntries.length > 0 ? (
            configEntries.map(([key, value]) => (
              <div key={key} className="bg-slate-800/60 border border-slate-700/60 rounded-xl p-3">
                <div className="text-slate-500 text-xs mb-1 uppercase tracking-wide">{key}</div>
                <div className="font-mono text-sm text-slate-200 break-words">{formatConfigValue(value)}</div>
              </div>
            ))
          ) : (
            <div className="bg-slate-800/60 border border-slate-700/60 rounded-xl p-3 text-sm text-slate-500">
              No config registered for this alpha.
            </div>
          )}
        </div>
      </div>

      <div>
        <SectionHeader>Equity Curve</SectionHeader>
        <div className="bg-slate-800/60 border border-slate-700/60 rounded-xl p-4">
          <EquityChart data={equity} />
        </div>
      </div>

      <div>
        <SectionHeader>Open Positions <span className="ml-1.5 text-indigo-400 font-mono text-base">({positions.length})</span></SectionHeader>
        <PositionCard alphaId={id} positions={positions} />
      </div>

      <div>
        <SectionHeader>Trade History</SectionHeader>
        <TradeTable alphaId={id} trades={trades} totalTrades={stats.total_trades} columnSpecs={columnSpecs} />
      </div>
    </div>
  );
}

function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="text-sm font-semibold mb-3 text-slate-300 uppercase tracking-widest flex items-center gap-2">
      <span className="w-3 h-px bg-slate-700 inline-block" />
      {children}
    </h2>
  );
}
