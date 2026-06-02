# Paper Trade Performance Best Practices

Date: 2026-06-01

## Runtime Defaults

- Keep `LOG_LEVEL=INFO` in normal runtime. Use `DEBUG` only during incident investigation or benchmark profiling.
- Keep worker auto TP/SL disabled in MDS mode: `ENABLE_WORKER_TPSL_AUTO_CLOSE=false`.
- Tune worker stream batching with `REDIS_READ_COUNT` and `REDIS_BLOCK_MS`; default is `100` and `1000`.
- Keep signal retention disabled by default with `SIGNAL_RETENTION_DAYS=0`; enable retention only after confirming audit requirements.

## Redis And Worker

- Use native async Redis clients in long-running worker loops. Avoid wrapping Redis calls in `asyncio.to_thread`.
- Ack stream messages only after the signal transaction has committed or after the processing error has been recorded.
- Process each signal in one SQLite transaction: log signal, apply execution, mark processed, commit.
- Keep Redis stream payloads stable. Performance changes must not alter alpha signal wire shape.

## SQLite

- Use WAL mode with `synchronous=NORMAL`, `busy_timeout=5000`, and `temp_store=MEMORY`.
- Add indexes for dashboard and worker lookup paths instead of compensating with in-memory filtering.
- Use `INSERT OR IGNORE` for idempotent alpha registration.
- For web reads, reuse a readonly `better-sqlite3` connection per process and cache dashboard responses for a short TTL.

## Alpha Runtime

- Candle upsert should fast-path last-candle replace and append; only use binary-search insertion for out-of-order corrections.
- Do not refresh price-alert subscriptions on every idle Pub/Sub timeout. Refresh on position changes and periodic sync only.
- Prefer optional fast JSON (`orjson`) with stdlib fallback so local/dev environments still work.
- Use `uvloop` when available in Linux containers, but keep startup safe when the package is absent.

## Indicators

- Avoid copying full candle arrays during every scan.
- For V5-family live scans, compute only the tail values needed for the current decision: `acol`, `acol_prev`, ATR, POC, close/high/low.
- Full replay is acceptable during warmup/recovery because it is not on the hot scan path.
- Any incremental indicator optimization must be tested against the previous full recompute output.

## Benchmarks

Run:

```bash
python3 scripts/perf/run_benchmarks.py --bench all --iterations 100
```

The benchmark output is JSONL under `benchmarks/results/` and includes p50/p95/p99 latency, throughput, and RSS delta.

Default gates:

- System runtime: p95 latency at most 50% of baseline or throughput at least 2x baseline.
- Base kline ingest: CPU time at most 35% of baseline.
- V5 scan: CPU time at most 25% of baseline and peak memory at most 60% of baseline.
- No phase should increase peak memory by more than 10% unless latency improves clearly.

## MDS Boundary

This paper-trade system consumes MDS but does not optimize MDS. Do not change MDS channels or payload contracts from paper-trade optimization work. MDS-specific optimizations should be planned and benchmarked separately.
