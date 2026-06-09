import { getAllTrades, getAlphaColumns } from "@/lib/db";
import type { ColumnSpec, Trade } from "@/lib/types";
import { withRoute } from "@/lib/api";

export const dynamic = "force-dynamic";

function escapeCsv(value: unknown): string {
  const text = value == null ? "" : String(value);
  return text.includes(",") || text.includes('"') || text.includes("\n")
    ? `"${text.replace(/"/g, '""')}"`
    : text;
}

function tradesToCsv(trades: Trade[], columnSpecs: ColumnSpec[]): string {
  const customHeaders = columnSpecs.map((column) => column.label);
  const headers = [
    "trade_id", "symbol", "side", "entry_price", "exit_price", "qty",
    "leverage", "tp", "sl", "pnl", "pnl_percent", "fee",
    ...customHeaders,
    "reason", "duration_hours", "opened_at", "closed_at",
  ];

  const rows = trades.map((trade) => {
    let metadata: Record<string, unknown> = {};
    try {
      metadata = JSON.parse(trade.metadata || "{}");
    } catch {}

    return [
      trade.trade_id, trade.symbol, trade.side, trade.entry_price, trade.exit_price, trade.qty,
      trade.leverage, trade.tp, trade.sl, trade.pnl, trade.pnl_percent, trade.fee,
      ...columnSpecs.map((column) => metadata[column.key]),
      trade.reason, trade.duration_hours, trade.opened_at, trade.closed_at,
    ].map(escapeCsv).join(",");
  });

  return [headers.map(escapeCsv).join(","), ...rows].join("\n");
}

export const GET = withRoute("api/trades/export", async (request) => {
  const alphaId = new URL(request.url).searchParams.get("alpha_id");
  if (!alphaId) {
    return Response.json({ error: "alpha_id required" }, { status: 400 });
  }

  const csv = tradesToCsv(getAllTrades(alphaId), getAlphaColumns(alphaId));
  const safeAlphaId = alphaId.replace(/[^a-zA-Z0-9_-]/g, "_");
  const date = new Date().toISOString().slice(0, 10);

  return new Response(csv, {
    headers: {
      "Content-Type": "text/csv; charset=utf-8",
      "Content-Disposition": `attachment; filename="trades-${safeAlphaId}-${date}.csv"`,
      "Cache-Control": "no-store",
    },
  });
});
