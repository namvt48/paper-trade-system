"use client";

import { useEffect, useRef, useState } from "react";

export interface PositionTick {
  symbol: string;
  exchange?: string;
  price: number;
  bid?: number | null;
  ask?: number | null;
  last?: number | null;
}

export function tickKey(exchange: string | undefined, symbol: string) {
  return `${exchange || "binance"}:${symbol}`;
}

/**
 * Subscribe to /api/position-ticks SSE stream with proper lifecycle handling.
 *
 * Critical: closes the EventSource on `pagehide` (browser back/forward cache =
 * bfcache freeze) so the connection does not leak and saturate the browser's
 * HTTP/1.1 connection pool. Reopens on `pageshow` when restored from bfcache.
 *
 * @param alphaId - omit to subscribe to ALL open positions across every alpha
 * @param enabled - when false, no stream is opened (default true)
 */
export function usePositionTicks(alphaId?: string, enabled = true) {
  const [ticks, setTicks] = useState<Record<string, PositionTick>>({});
  const pendingTicks = useRef<Record<string, PositionTick>>({});

  useEffect(() => {
    if (!enabled) return;
    let events: EventSource | null = null;
    let flushTimer: ReturnType<typeof setInterval> | undefined;
    let closed = false;

    const url = alphaId
      ? `/api/position-ticks?alpha_id=${encodeURIComponent(alphaId)}`
      : "/api/position-ticks";

    const open = () => {
      if (events || closed) return;
      events = new EventSource(url);

      flushTimer = setInterval(() => {
        const pending = pendingTicks.current;
        if (Object.keys(pending).length === 0) return;
        pendingTicks.current = {};
        setTicks((current) => ({ ...current, ...pending }));
      }, 250);

      const onTick = (event: Event) => {
        try {
          const tick = JSON.parse((event as MessageEvent<string>).data) as PositionTick;
          if (!tick.symbol || !Number.isFinite(tick.price)) return;
          pendingTicks.current[tickKey(tick.exchange, tick.symbol)] = tick;
        } catch {
          // Ignore malformed ticker messages and keep the live stream running.
        }
      };

      events.addEventListener("tick", onTick);
    };

    const close = () => {
      if (flushTimer) {
        clearInterval(flushTimer);
        flushTimer = undefined;
      }
      if (events) {
        events.close();
        events = null;
      }
    };

    open();

    // Chromium bfcache: when navigating back/forward, the browser freezes the
    // page but does NOT fire React unmount, so EventSource connections leak and
    // saturate the HTTP/1.1 connection pool (6 per origin). Firefox handles
    // this gracefully; Chrome/Brave does not.
    //
    // Close on visibilitychange=hidden (fires early during navigation freeze)
    // AND on pagehide (bfcache freeze). Reopen on pageshow (bfcache restore)
    // and visibilitychange=visible (tab refocus).
    const onPageHide = () => close();
    const onPagesShow = (event: PageTransitionEvent) => {
      if (event.persisted) open();
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === "hidden") close();
      else if (document.visibilityState === "visible") open();
    };

    window.addEventListener("pagehide", onPageHide);
    window.addEventListener("pageshow", onPagesShow);
    document.addEventListener("visibilitychange", onVisibilityChange);

    return () => {
      closed = true;
      close();
      window.removeEventListener("pagehide", onPageHide);
      window.removeEventListener("pageshow", onPagesShow);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [alphaId, enabled]);

  return ticks;
}
