# CLAUDE.md — paper-trade-system

You are operating inside the PAPER trading repo (virtual accounts, worker, alpha runners, PM).
This is a multi-system workspace. **Read `../docs/system-map.md` first** — it is the single
source of truth (services, ports, server paths, Makefile deploy targets, equity semantics).

- Deploy via this repo's own Makefile only; server dir `/root/paper-trade-system`.
- Never touch the LIVE system: `/root/trading-system/data/*` is off-limits, and `/live/*`
  dashboard pages do not read this repo.
- `worker/app/equity_snapshots.py` is the reference collector (capital + realized + unrealized).
- Alpha deploys → skill `paper-trade-alpha-deploy`; TCBS token → skill `tcbs-key-rotation`.