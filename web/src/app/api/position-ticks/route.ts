import { randomUUID } from "node:crypto";
import { createClient } from "redis";
import { getOpenPositions } from "@/lib/db";
import { createLogger } from "@/lib/logger";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const log = createLogger("api/position-ticks");
const MDS_REDIS_URL = process.env.MDS_REDIS_URL || "redis://localhost:6381";
const SNAPSHOT_TFS = ["1m", "5m", "15m", "30m", "1h", "4h", "12h"];

interface SnapshotTick {
  symbol: string;
  exchange: string;
  timestamp: number;
  price: number;
  price_type: string;
  bid: null;
  ask: null;
  last: number;
  source: string;
}

interface SnapshotCandle {
  symbol?: unknown;
  tf?: unknown;
  close?: unknown;
  close_time?: unknown;
  exchange?: unknown;
}

interface SnapshotRedisClient {
  lIndex(key: string, index: number): Promise<string | null>;
}

function encodeEvent(encoder: TextEncoder, event: string, data: unknown) {
  return encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}

function snapshotListKey(exchange: string, tf: string, symbol: string) {
  return `kline_snapshot_v2:${exchange}:${tf}:${symbol}`;
}

function toFiniteNumber(value: unknown): number | null {
  const n = typeof value === "number" ? value : typeof value === "string" ? Number(value) : NaN;
  return Number.isFinite(n) ? n : null;
}

function parseSnapshotTick(raw: string | null, exchange: string, symbol: string): SnapshotTick | null {
  if (!raw) return null;
  try {
    const candle = JSON.parse(raw) as SnapshotCandle;
    const price = toFiniteNumber(candle.close);
    if (price == null || price <= 0) return null;
    const timestamp = toFiniteNumber(candle.close_time) ?? Date.now();
    const tf = typeof candle.tf === "string" && candle.tf ? candle.tf : "unknown";
    return {
      symbol,
      exchange: typeof candle.exchange === "string" && candle.exchange ? candle.exchange : exchange,
      timestamp,
      price,
      price_type: "snapshot_close",
      bid: null,
      ask: null,
      last: price,
      source: `kline_snapshot:${tf}`,
    };
  } catch {
    return null;
  }
}

async function latestSnapshotTick(commandClient: SnapshotRedisClient, exchange: string, symbol: string) {
  const ticks = await Promise.all(
    SNAPSHOT_TFS.map(async (tf) => {
      const raw = await commandClient.lIndex(snapshotListKey(exchange, tf, symbol), 0);
      return parseSnapshotTick(raw, exchange, symbol);
    })
  );
  return ticks
    .filter((tick): tick is SnapshotTick => tick != null)
    .sort((a, b) => b.timestamp - a.timestamp)[0] || null;
}

async function enqueueInitialSnapshotTicks(
  controller: ReadableStreamDefaultController<Uint8Array>,
  encoder: TextEncoder,
  commandClient: SnapshotRedisClient,
  symbolsByExchange: Map<string, Set<string>>,
  alphaId: string,
  isClosed: () => boolean
) {
  let emitted = 0;
  for (const [exchange, symbols] of symbolsByExchange) {
    for (const symbol of symbols) {
      if (isClosed()) return emitted;
      try {
        const tick = await latestSnapshotTick(commandClient, exchange, symbol);
        if (!tick || isClosed()) continue;
        controller.enqueue(encodeEvent(encoder, "tick", tick));
        emitted += 1;
      } catch (error) {
        log.debug("failed to load initial position snapshot tick", {
          alpha_id: alphaId,
          exchange,
          symbol,
          err: String(error),
        });
      }
    }
  }
  return emitted;
}

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const alphaId = searchParams.get("alpha_id") || undefined;

  const consumerId = `paper-web:${alphaId || "all"}:${randomUUID()}`;
  const logAlphaId = alphaId || "all";

  const symbolsByExchange = new Map<string, Set<string>>();
  for (const position of getOpenPositions(alphaId)) {
    const exchange = position.exchange || "binance";
    const symbols = symbolsByExchange.get(exchange) || new Set<string>();
    symbols.add(position.symbol);
    symbolsByExchange.set(exchange, symbols);
  }
  const channels = [...symbolsByExchange].flatMap(([exchange, symbols]) =>
    [...symbols].map((symbol) => `price_alert:${exchange}:${symbol}`)
  );

  const encoder = new TextEncoder();
  const subscriber = createClient({ url: MDS_REDIS_URL });
  const commandClient = createClient({ url: MDS_REDIS_URL });
  let heartbeat: ReturnType<typeof setInterval> | undefined;
  let subscriptionRefresh: ReturnType<typeof setInterval> | undefined;
  let closed = false;

  const publishSubscriptionSync = async (clear = false) => {
    await Promise.all([...symbolsByExchange].map(([exchange, symbols]) =>
      commandClient.publish(
        `price_alert:subscribe:${exchange}`,
        JSON.stringify({
          consumer_id: consumerId,
          action: "sync",
          symbols: clear ? [] : [...symbols],
        })
      )
    ));
  };

  const closeClients = async () => {
    if (closed) return;
    closed = true;
    if (heartbeat) clearInterval(heartbeat);
    if (subscriptionRefresh) clearInterval(subscriptionRefresh);
    if (commandClient.isOpen) {
      await publishSubscriptionSync(true).catch((error) => {
        log.error("failed to clear MDS price alert subscription", {
          alpha_id: logAlphaId,
          err: String(error),
        });
      });
      await commandClient.close().catch(() => commandClient.destroy());
    }
    if (subscriber.isOpen) {
      await subscriber.close().catch(() => subscriber.destroy());
    }
  };

  const stream = new ReadableStream({
    async start(controller) {
      request.signal.addEventListener("abort", () => void closeClients(), { once: true });
      subscriber.on("error", (error) => {
        log.error("MDS Redis subscriber error", { alpha_id: logAlphaId, err: String(error) });
      });
      commandClient.on("error", (error) => {
        log.error("MDS Redis command client error", { alpha_id: logAlphaId, err: String(error) });
      });

      try {
        controller.enqueue(encodeEvent(encoder, "connected", { channels: channels.length }));

        if (channels.length > 0) {
          await Promise.all([subscriber.connect(), commandClient.connect()]);
          if (closed) {
            subscriber.destroy();
            commandClient.destroy();
            return;
          }
          const initialTicks = await enqueueInitialSnapshotTicks(
            controller,
            encoder,
            commandClient,
            symbolsByExchange,
            logAlphaId,
            () => closed
          );
          if (initialTicks > 0) {
            log.debug("sent initial position snapshot ticks", {
              alpha_id: logAlphaId,
              ticks: initialTicks,
              channels: channels.length,
            });
          }
          await subscriber.subscribe(channels, (message) => {
            if (closed) return;
            try {
              controller.enqueue(encodeEvent(encoder, "tick", JSON.parse(message)));
            } catch (error) {
              log.error("invalid MDS price alert", { alpha_id: logAlphaId, err: String(error) });
            }
          });
          await publishSubscriptionSync();
          subscriptionRefresh = setInterval(() => {
            void publishSubscriptionSync().catch((error) => {
              log.error("failed to refresh MDS price alert subscription", {
                alpha_id: logAlphaId,
                err: String(error),
              });
            });
          }, 30000);
        }

        heartbeat = setInterval(() => {
          if (!closed) {
            controller.enqueue(encodeEvent(encoder, "ping", { ts: Date.now() }));
          }
        }, 30000);
      } catch (error) {
        log.error("failed to subscribe to MDS price alerts", {
          alpha_id: logAlphaId,
          channels: channels.length,
          err: String(error),
        });
        if (!closed) {
          controller.enqueue(encodeEvent(encoder, "stream-error", { error: "ticker stream unavailable" }));
          controller.close();
        }
        await closeClients();
      }
    },
    async cancel() {
      await closeClients();
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
    },
  });
}
