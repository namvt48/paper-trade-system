import { NextResponse } from "next/server";
import { createLogger } from "./logger";

const log = createLogger("api");

type Handler = (req: Request) => Promise<Response> | Response;

export function withRoute(mod: string, handler: Handler): Handler {
  return async (req: Request) => {
    const t0 = performance.now();
    const url = new URL(req.url);
    try {
      const res = await handler(req);
      log.info("request", {
        mod,
        method: req.method,
        path: url.pathname,
        search: url.search || undefined,
        status: res.status,
        dur_ms: Math.round(performance.now() - t0),
      });
      return res;
    } catch (err) {
      log.error("unhandled error", {
        mod,
        method: req.method,
        path: url.pathname,
        err: String(err),
        dur_ms: Math.round(performance.now() - t0),
      });
      return NextResponse.json({ error: "internal server error" }, { status: 500 });
    }
  };
}
