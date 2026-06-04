import { randomUUID } from "node:crypto";
import { createClient } from "redis";
import { getOpenPositions } from "@/lib/db";
import { createLogger } from "@/lib/logger";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const log = createLogger("api/position-ticks");
const MDS_REDIS_URL = process.env.MDS_REDIS_URL || "redis://localhost:6381";

function encodeEvent(encoder: TextEncoder, event: string, data: unknown) {
  return encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const alphaId = searchParams.get("alpha_id");

  if (!alphaId) {
    return Response.json({ error: "alpha_id is required" }, { status: 400 });
  }

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
  const consumerId = `paper-web:${alphaId}:${randomUUID()}`;
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
          alpha_id: alphaId,
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
        log.error("MDS Redis subscriber error", { alpha_id: alphaId, err: String(error) });
      });
      commandClient.on("error", (error) => {
        log.error("MDS Redis command client error", { alpha_id: alphaId, err: String(error) });
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
          await subscriber.subscribe(channels, (message) => {
            if (closed) return;
            try {
              controller.enqueue(encodeEvent(encoder, "tick", JSON.parse(message)));
            } catch (error) {
              log.error("invalid MDS price alert", { alpha_id: alphaId, err: String(error) });
            }
          });
          await publishSubscriptionSync();
          subscriptionRefresh = setInterval(() => {
            void publishSubscriptionSync().catch((error) => {
              log.error("failed to refresh MDS price alert subscription", {
                alpha_id: alphaId,
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
          alpha_id: alphaId,
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
