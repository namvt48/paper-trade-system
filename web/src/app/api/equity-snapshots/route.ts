import { NextResponse } from "next/server";
import { getEquitySnapshots, isSnapshotDbAvailable } from "@/lib/equity-snapshots";
import { withRoute } from "@/lib/api";

export const dynamic = "force-dynamic";

export const GET = withRoute("api/equity-snapshots", async (request) => {
  if (!isSnapshotDbAvailable()) {
    return NextResponse.json({ error: "equity snapshots database not available", data: [] }, { status: 200 });
  }

  const { searchParams } = new URL(request.url);
  const alphaId = searchParams.get("alpha_id") || "";
  const maxPoints = Number(searchParams.get("max_points") || "1000");

  const data = getEquitySnapshots(alphaId, maxPoints);
  return NextResponse.json({ data, alpha_id: alphaId || "__TOTAL__", count: data.length });
});
