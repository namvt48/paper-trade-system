"use client";

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
import type { EquityPoint } from "@/lib/types";

interface EquityChartProps {
  data: EquityPoint[];
  color?: string;
  height?: number;
}

export function EquityChart({ data, color = "#22c55e", height = 300 }: EquityChartProps) {
  if (data.length === 0) {
    return <div className="text-slate-500 text-center py-8">No equity data yet</div>;
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data}>
        <CartesianGrid strokeDasharray="3 3" stroke="#475569" />
        <XAxis
          dataKey="closed_at"
          tick={{ fontSize: 11, fill: "#94a3b8" }}
          tickFormatter={(v: string) => new Date(v).toLocaleDateString()}
        />
        <YAxis tick={{ fontSize: 11, fill: "#94a3b8" }} />
        <Tooltip
          contentStyle={{ backgroundColor: "#1e293b", border: "1px solid #475569" }}
          labelFormatter={(v: string) => new Date(v).toLocaleString()}
        />
        <ReferenceLine y={0} stroke="#64748b" strokeDasharray="3 3" />
        <Line type="monotone" dataKey="equity" stroke={color} dot={false} strokeWidth={2} />
      </LineChart>
    </ResponsiveContainer>
  );
}

interface CompareChartProps {
  datasets: Record<string, EquityPoint[]>;
}

const COLORS = ["#22c55e", "#3b82f6", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899"];

export function CompareChart({ datasets }: CompareChartProps) {
  const keys = Object.keys(datasets);
  if (keys.length === 0) return <div className="text-slate-500 text-center py-8">No data</div>;

  const merged = new Map<string, Record<string, number>>();
  for (const [alphaId, points] of Object.entries(datasets)) {
    for (const p of points) {
      const existing = merged.get(p.closed_at) || {};
      existing[alphaId] = p.equity;
      merged.set(p.closed_at, existing);
    }
  }
  const chartData = Array.from(merged.entries())
    .map(([time, values]) => ({ time, ...values }))
    .sort((a, b) => a.time.localeCompare(b.time));

  return (
    <ResponsiveContainer width="100%" height={350}>
      <LineChart data={chartData}>
        <CartesianGrid strokeDasharray="3 3" stroke="#475569" />
        <XAxis
          dataKey="time"
          tick={{ fontSize: 11, fill: "#94a3b8" }}
          tickFormatter={(v: string) => new Date(v).toLocaleDateString()}
        />
        <YAxis tick={{ fontSize: 11, fill: "#94a3b8" }} />
        <Tooltip contentStyle={{ backgroundColor: "#1e293b", border: "1px solid #475569" }} />
        <ReferenceLine y={0} stroke="#64748b" strokeDasharray="3 3" />
        {keys.map((key, i) => (
          <Line key={key} type="monotone" dataKey={key} stroke={COLORS[i % COLORS.length]} dot={false} strokeWidth={2} />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}
