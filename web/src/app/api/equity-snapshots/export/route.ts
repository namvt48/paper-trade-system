import { getRawEquitySnapshots, isSnapshotDbAvailable } from "@/lib/equity-snapshots";
import type { EquitySnapshot } from "@/lib/types";
import { withRoute } from "@/lib/api";

export const dynamic = "force-dynamic";

function escapeCsv(value: unknown): string {
  const text = value == null ? "" : String(value);
  return text.includes(",") || text.includes('"') || text.includes("\n")
    ? `"${text.replace(/"/g, '""')}"`
    : text;
}

function snapshotsToCsv(snapshots: EquitySnapshot[]): string {
  const headers = ["timestamp", "balance"];
  const rows = snapshots.map((s) => [s.timestamp, s.balance].map(escapeCsv).join(","));
  return [headers.map(escapeCsv).join(","), ...rows].join("\n");
}

export const GET = withRoute("api/equity-snapshots/export", async (request) => {
  if (!isSnapshotDbAvailable()) {
    return Response.json({ error: "equity snapshots database not available" }, { status: 404 });
  }

  const { searchParams } = new URL(request.url);
  const alphaId = searchParams.get("alpha_id") || "";

  const snapshots = getRawEquitySnapshots(alphaId);
  const csv = snapshotsToCsv(snapshots);
  const label = alphaId || "total";
  const safeLabel = label.replace(/[^a-zA-Z0-9_-]/g, "_");
  const date = new Date().toISOString().slice(0, 10);

  return new Response(csv, {
    headers: {
      "Content-Type": "text/csv; charset=utf-8",
      "Content-Disposition": `attachment; filename="equity-snapshots-${safeLabel}-${date}.csv"`,
      "Cache-Control": "no-store",
    },
  });
});
