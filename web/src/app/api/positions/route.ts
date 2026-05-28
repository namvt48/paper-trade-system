import { NextResponse } from "next/server";
import { getOpenPositions } from "@/lib/db";
import { withRoute } from "@/lib/api";

export const GET = withRoute("api/positions", async (request) => {
  const { searchParams } = new URL(request.url);
  const alphaId = searchParams.get("alpha_id") || undefined;
  return NextResponse.json(getOpenPositions(alphaId));
});
