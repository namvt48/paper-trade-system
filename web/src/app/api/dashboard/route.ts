import { NextResponse } from "next/server";
import { getDashboardData } from "@/lib/db";
import { withRoute } from "@/lib/api";

export const GET = withRoute("api/dashboard", async () => {
  return NextResponse.json(getDashboardData());
});
