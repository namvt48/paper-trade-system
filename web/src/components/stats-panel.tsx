import type { AlphaStats } from "@/lib/types";

interface StatsPanelProps {
  stats: AlphaStats;
}

export function StatsPanel({ stats }: StatsPanelProps) {
  const items = [
    { label: "Total Trades", value: stats.total_trades },
    { label: "Win / Loss", value: `${stats.win_trades} / ${stats.loss_trades}` },
    { label: "Winrate", value: `${stats.winrate.toFixed(1)}%` },
    { label: "Total PnL", value: stats.total_pnl.toFixed(4), highlight: stats.total_pnl > 0 },
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
