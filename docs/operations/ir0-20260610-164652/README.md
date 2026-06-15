# IR0 PTS Recovery Evidence - 2026-06-10 16:46:52 +07:00

## Scope and Change Policy

This capture was taken before any restart, position close, stream deletion, or runtime
ownership change. New runtime changes remain frozen until the recovery action is
approved.

## Backups

| Artifact | SHA-256 |
|---|---|
| `backups/paper-trade.db` | `1ec9f383bfee144bc0fd5470dfcd83161a28bf74b38604c541ff9b9ee0a3a466` |
| `backups/paper-redis-dump.rdb` | `6ca122838e6ea2333940b562efd91d1351e0a05192e57991419e823ddd9e160d` |
| `backups/paper-redis-appendonlydir/appendonly.aof.1.base.rdb` | `a02624173dbcedd742a91c3dff148c9b731928ded611deecec0401a42c116756` |
| `backups/paper-redis-appendonlydir/appendonly.aof.1.incr.aof` | `bda8239cc2b0f7dfa157a761798f7856bf0df49d46a74346fad30aea40fa207a` |
| `backups/paper-redis-appendonlydir/appendonly.aof.manifest` | `ca465c5845ad4d4c0a2b4cda8853efeb9423cb4242a4eaaa5ecbaa00a4e75492` |

SQLite `PRAGMA integrity_check` returned `ok`.

## Runtime Inventory

- Worker: running but unhealthy.
- Paper Redis: healthy, `paper-signals` group `paper-executor` has `pending=0`,
  `lag=0`.
- Source compose expects central `redis-net`, but runtime still uses the old
  `paper-trade` and `market-data` networks. `redis-net` does not exist.
- Worker image was created 2026-06-10; q1/q2/q3/v5b alpha images were created
  2026-06-02 and Hyper Turbo on 2026-06-08.
- No `paper:alpha-runtime:*` heartbeat exists for any alpha with an open position.
- Root cause of `runtime_not_live`: alpha runtime images predate the heartbeat and
  authoritative position-reconcile implementation now used by the worker.

Configured `REGISTERED_ALPHAS`:

`alpha-1-v5b, alpha-1-v5b-5pct, alpha-1-bangoc, alpha-1-v5b-reverse, hyper-turbo`

Running alphas with authoritative positions:

`alpha-1-q1, alpha-1-q2, alpha-1-q3, alpha-1-v5b, hyper-turbo`

q1/q2/q3 are running but absent from `REGISTERED_ALPHAS`.

## Authoritative Position Classification

All 12 open positions are classified **recoverable** because a matching strategy
runtime exists and current source can deserialize the authoritative legacy snapshot.
None is currently `owned`; none is classified orphaned. Therefore no close-only
action has been prepared yet.

| Alpha | Positions | Symbols | Current MDS subscription coverage |
|---|---:|---|---|
| `alpha-1-q1` | 1 | HOMEUSDT | none |
| `alpha-1-q2` | 4 | AGTUSDT, MERLUSDT, PTBUSDT, RONINUSDT | AGTUSDT, RONINUSDT |
| `alpha-1-q3` | 1 | CARVUSDT | CARVUSDT |
| `alpha-1-v5b` | 5 | BOMEUSDT, LPTUSDT, MERLUSDT, RONINUSDT, SKLUSDT | BOMEUSDT, RONINUSDT |
| `hyper-turbo` | 1 | BTCUSDT | BTCUSDT |

Hyper Turbo's BTC position has no fixed SL/TP by strategy design. Its recovered
runtime defaults are covered by the base normalization and Hyper Turbo state-machine
tests, but it still requires supervised ownership verification after restart.

## Code Changes Completed

- Ownership mismatch logs now emit on state transition and every five-minute summary,
  instead of every five seconds.
- Consumer-group creation ignores only Redis `BUSYGROUP`; auth and other Redis errors
  surface.
- Worker suite: `130 passed`.
- Base legacy reconcile test: `1 passed`.
- Hyper Turbo engine tests: `2 passed`.

## Pending Recovery Action

Rebuild and restart one alpha at a time from current source, beginning with a
single-position alpha. Before doing so, provide connectivity compatible with current
source or explicitly retain the old runtime network. After each restart, verify:

1. `runtime_state=LIVE` heartbeat exists.
2. Managed position IDs exactly match the authoritative snapshot.
3. Desired and actual MDS subscriptions cover every symbol.
4. No duplicate OPEN/CLOSE/MODIFY signal is produced.
5. Worker health improves without weakening the ownership invariant.
