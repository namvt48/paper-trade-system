"use client";

import { useCallback, useEffect, useState } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  ReferenceLine,
} from "recharts";
import type { EquitySnapshot } from "@/lib/types";

interface LiveEquityChartProps {
  alphaId?: string;
  height?: number;
}

const POLL_INTERVAL_MS = 15 * 60 * 1000;

export function LiveEquityChart({ alphaId = "", height = 300 }: LiveEquityChartProps) {
  const [data, setData] = useState<EquitySnapshot[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (alphaId) params.set("alpha_id", alphaId);
      const res = await fetch(`/api/equity-snapshots?${params.toString()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setData(json.data || []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "fetch failed");
    } finally {
      setLoading(false);
    }
  }, [alphaId]);

  useEffect(() => {
    fetchData();
    const timer = setInterval(fetchData, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [fetchData]);

  const exportUrl = `/api/equity-snapshots/export${alphaId ? `?alpha_id=${encodeURIComponent(alphaId)}` : ""}`;

  if (loading) {
    return <div className="text-slate-500 text-center py-8">Loading equity snapshots...</div>;
  }

  if (error) {
    return <div className="text-rose-400 text-center py-8">Error: {error}</div>;
  }

  if (data.length === 0) {
    return <div className="text-slate-500 text-center py-8">No snapshot data yet. Worker will start collecting in ~15 minutes.</div>;
  }

  const startBalance = data[0]?.balance ?? 0;
  const lastBalance = data[data.length - 1]?.balance ?? 0;
  const change = lastBalance - startBalance;
  const changePercent = startBalance !== 0 ? (change / startBalance) * 100 : 0;
  const isPositive = change >= 0;

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-4">
          <div>
            <span className="text-slate-400 text-xs uppercase tracking-wide">Balance</span>
            <div className="font-mono text-lg font-bold text-slate-100">
              ${lastBalance.toFixed(2)}
            </div>
          </div>
          <div>
            <span className="text-slate-400 text-xs uppercase tracking-wide">Change</span>
            <div className={`font-mono text-sm font-semibold ${isPositive ? "text-emerald-400" : "text-rose-400"}`}>
              {isPositive ? "+" : ""}{change.toFixed(2)} ({isPositive ? "+" : ""}{changePercent.toFixed(2)}%)
            </div>
          </div>
        </div>
        <a
          href={exportUrl}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-slate-300 bg-slate-700/60 hover:bg-slate-700 border border-slate-600/60 rounded-lg transition-colors"
        >
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
          </svg>
          Download CSV
        </a>
      </div>
      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={data}>
          <CartesianGrid strokeDasharray="3 3" stroke="#475569" />
          <XAxis
            dataKey="timestamp"
            tick={{ fontSize: 11, fill: "#94a3b8" }}
            tickFormatter={(v: string) => {
              const d = new Date(v);
              return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
            }}
          />
          <YAxis
            tick={{ fontSize: 11, fill: "#94a3b8" }}
            domain={["auto", "auto"]}
            tickFormatter={(v: number) => `$${v.toFixed(0)}`}
          />
          <Tooltip
            contentStyle={{ backgroundColor: "#1e293b", border: "1px solid #475569", borderRadius: "8px" }}
            labelFormatter={(v: string) => new Date(v).toLocaleString()}
            formatter={(value: number) => [`$${value.toFixed(2)}`, "Balance"]}
          />
          <ReferenceLine y={startBalance} stroke="#64748b" strokeDasharray="3 3" />
          <Line type="monotone" dataKey="balance" stroke="#22c55e" dot={false} strokeWidth={2} />
        </LineChart>
      </ResponsiveContainer>
      <div className="flex items-center gap-4 mt-2 text-xs text-slate-500">
        <span>{data.length} data points</span>
        <span>Auto-updates every 15 min</span>
        <span>Interval auto-scales: 5m → 15m → 30m → 1h</span>
      </div>
    </div>
  );
}
