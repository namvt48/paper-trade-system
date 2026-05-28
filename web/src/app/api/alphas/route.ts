import { NextResponse } from "next/server";
import { getAllAlphas, getAlpha } from "@/lib/db";
import { withRoute } from "@/lib/api";

export const GET = withRoute("api/alphas", async (request) => {
  const { searchParams } = new URL(request.url);
  const id = searchParams.get("id");

  if (id) {
    const alpha = getAlpha(id);
    if (!alpha) return NextResponse.json({ error: "not found" }, { status: 404 });
    return NextResponse.json(alpha);
  }

  return NextResponse.json(getAllAlphas());
});
