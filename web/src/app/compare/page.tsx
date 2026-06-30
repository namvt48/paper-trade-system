import { getAllAlphas, getCompareEquity, getAlphaStats, getOpenPositions } from "@/lib/db";
import { CompareChart } from "@/components/equity-chart";
import { StatsPanel } from "@/components/stats-panel";
import type { EquityPoint } from "@/lib/types";

export const dynamic = "force-dynamic";

function parseSelectedIds(raw: string | string[] | undefined, fallback: string[]): string[] {
  if (!raw) return fallback;
  // Next.js may give string[] for repeated ?alphas=a&alphas=b, or a comma-joined string
  if (Array.isArray(raw)) return raw.filter(Boolean);
  return raw.split(",").filter(Boolean);
}

export default async function ComparePage({
  searchParams,
}: {
  searchParams: Promise<{ alphas?: string | string[] }>;
}) {
  const params = await searchParams;
  const alphas = getAllAlphas();
  const selectedIds = parseSelectedIds(params.alphas, alphas.slice(0, 3).map((a) => a.alpha_id));

  const equityData = getCompareEquity(selectedIds);
  const datasets: Record<string, EquityPoint[]> = {};
  for (const [id, points] of equityData) {
    datasets[id] = points;
  }

  return (
    <div className="space-y-8">
      <div className="flex items-end justify-between">
        <div>
          <p className="text-slate-400 text-xs uppercase tracking-widest mb-1">Analysis</p>
          <h1 className="text-2xl font-bold text-white">Compare Alphas</h1>
        </div>
      </div>

      <form className="flex flex-wrap gap-2">
        {alphas.map((a) => (
          <label
            key={a.alpha_id}
            className="flex items-center gap-2 bg-slate-800/60 border border-slate-700/60 rounded-lg px-3 py-2 text-sm cursor-pointer hover:border-indigo-700/60 transition-colors"
          >
            <input
              type="checkbox"
              name="alphas"
              value={a.alpha_id}
              defaultChecked={selectedIds.includes(a.alpha_id)}
              className="accent-indigo-500"
            />
            <span className="text-slate-300">{a.display_name}</span>
          </label>
        ))}
        <button
          type="submit"
          className="bg-indigo-600 hover:bg-indigo-500 text-white px-5 py-2 rounded-lg text-sm font-medium transition-colors"
        >
          Compare
        </button>
      </form>

      <div className="bg-slate-800/60 border border-slate-700/60 rounded-xl p-4">
        <CompareChart datasets={datasets} />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {selectedIds.map((id) => {
          const alpha = alphas.find((a) => a.alpha_id === id);
          const stats = getAlphaStats(id);
          const positions = getOpenPositions(id);
          return (
            <div key={id}>
              <h3 className="text-sm font-semibold mb-3 text-slate-300 uppercase tracking-widest flex items-center gap-2">
                <span className="w-3 h-px bg-slate-700 inline-block" />
                {alpha?.display_name ?? id}
              </h3>
              <StatsPanel stats={stats} positions={positions} alphaId={id} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
