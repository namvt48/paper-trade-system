import { NextResponse } from "next/server";
import { getEquityCurve, getCompareEquity } from "@/lib/db";
import { withRoute } from "@/lib/api";

export const GET = withRoute("api/equity", async (request) => {
  const { searchParams } = new URL(request.url);
  const alphaId = searchParams.get("alpha_id");
  const alphas = searchParams.get("alphas");

  if (alphas) {
    const ids = alphas.split(",").filter(Boolean);
    return NextResponse.json(Object.fromEntries(getCompareEquity(ids)));
  }

  if (!alphaId) {
    return NextResponse.json({ error: "alpha_id required" }, { status: 400 });
  }

  return NextResponse.json(getEquityCurve(alphaId));
});
