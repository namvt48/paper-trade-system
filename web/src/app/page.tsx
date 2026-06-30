import { getDashboardData } from "@/lib/db";
import { AlphaUnrealizedPnl, UnrealizedPnlProvider } from "@/components/alpha-unrealized-pnl";

export const dynamic = "force-dynamic";

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

export default async function DashboardPage() {
  const data = getDashboardData();
  const hasOpenPositions = data.alphas.some((a) => a.open_positions > 0);

  return (
    <div className="space-y-8">
      <div>
        <div>
          <p className="text-slate-400 text-xs uppercase tracking-widest mb-1">Overview</p>
          <h1 className="text-2xl font-bold text-white">Dashboard</h1>
        </div>
      </div>

      <UnrealizedPnlProvider hasOpenPositions={hasOpenPositions}>
        <div className="rounded-xl border border-slate-700/60 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm min-w-[800px]">
            <thead>
              <tr className="bg-slate-800/80 border-b border-slate-700/60 text-slate-400 text-xs uppercase tracking-wider">
                <th className="text-left py-3 px-4 w-10">#</th>
                <th className="text-left py-3 px-4">Alpha</th>
                <th className="text-left py-3 px-4">Status</th>
                <th className="text-right py-3 px-4">PnL</th>
                <th className="text-right py-3 px-4">Unrealized PnL</th>
                <th className="text-right py-3 px-4">Winrate</th>
                <th className="text-right py-3 px-4">Open</th>
                <th className="text-right py-3 px-4">Today</th>
                <th className="text-right py-3 px-4">Started (UTC)</th>
              </tr>
            </thead>
            <tbody>
              {data.alphas.map((a, i) => (
                <tr
                  key={a.alpha_id}
                  className={`border-b border-slate-700/40 hover:bg-slate-700/30 transition-colors ${i % 2 === 0 ? "" : "bg-slate-800/30"}`}
                >
                  <td className="py-3 px-4 font-mono text-slate-500 text-xs">{i + 1}</td>
                  <td className="py-3 px-4">
                    <a href={`/alpha/${a.alpha_id}`} className="text-indigo-300 hover:text-indigo-200 font-medium transition-colors">
                      {a.display_name}
                    </a>
                  </td>
                  <td className="py-3 px-4">
                    <span className={`inline-flex items-center gap-1.5 text-xs font-medium px-2 py-0.5 rounded-full border ${
                      a.status === "active"
                        ? "bg-emerald-900/50 border-emerald-600/50 text-emerald-300"
                        : "bg-slate-700/50 border-slate-600/50 text-slate-300"
                    }`}>
                      <span className={`w-1.5 h-1.5 rounded-full ${a.status === "active" ? "bg-emerald-400" : "bg-slate-400"}`} />
                      {a.status || "unknown"}
                    </span>
                  </td>
                  <td className={`py-3 px-4 text-right font-mono font-semibold ${a.pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                    {a.pnl >= 0 ? "+" : ""}{a.pnl.toFixed(4)}
                  </td>
                  <td className="py-3 px-4 text-right">
                    <AlphaUnrealizedPnl alphaId={a.alpha_id} />
                  </td>
                  <td className="py-3 px-4 text-right font-mono text-slate-200">{a.winrate.toFixed(1)}%</td>
                  <td className="py-3 px-4 text-right">
                    {a.open_positions > 0
                      ? <span className="bg-indigo-900/50 border border-indigo-600/50 text-indigo-200 font-mono text-xs px-2 py-0.5 rounded">{a.open_positions}</span>
                      : <span className="text-slate-500">—</span>
                    }
                  </td>
                  <td className="py-3 px-4 text-right font-mono text-slate-200">{a.today_trades || "—"}</td>
                  <td className="py-3 px-4 text-right font-mono text-slate-400 text-xs">{fmtDate(a.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </div>
      </UnrealizedPnlProvider>

    </div>
  );
}
