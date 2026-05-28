import { NextResponse } from "next/server";
import { getTrades, getAlphaStats } from "@/lib/db";
import { withRoute } from "@/lib/api";

export const GET = withRoute("api/trades", async (request) => {
  const { searchParams } = new URL(request.url);
  const alphaId = searchParams.get("alpha_id");
  const limit = parseInt(searchParams.get("limit") || "100");
  const offset = parseInt(searchParams.get("offset") || "0");

  if (searchParams.get("stats") === "1" && alphaId) {
    return NextResponse.json(getAlphaStats(alphaId));
  }

  if (!alphaId) {
    return NextResponse.json({ error: "alpha_id required" }, { status: 400 });
  }

  return NextResponse.json(getTrades(alphaId, limit, offset));
});
