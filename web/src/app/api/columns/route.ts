import { NextResponse } from "next/server";
import { getAlphaColumns } from "@/lib/db";
import { withRoute } from "@/lib/api";

export const GET = withRoute("api/columns", async (request) => {
  const { searchParams } = new URL(request.url);
  const alphaId = searchParams.get("alpha_id");

  if (!alphaId) {
    return NextResponse.json({ error: "alpha_id required" }, { status: 400 });
  }

  return NextResponse.json(getAlphaColumns(alphaId));
});
