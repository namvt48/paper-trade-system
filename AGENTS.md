# AGENTS.md — paper-trade-system

This repo runs PAPER trading: virtual accounts, signal worker, alpha runners, portfolio manager.
It is one repo in a multi-system workspace — **the authoritative system map is
`../docs/system-map.md`** (golden rules, URL→repo routing, services table, Makefile targets,
equity semantics). Read it before any cross-repo question or deploy.

Golden rules that bite here:
- Deploy ONLY via this repo's Makefile (`make deploy`, `deploy-runner`, `deploy-runner-config`,
  `deploy-restart`). Server dir: `/root/paper-trade-system`.
- Do NOT touch `/root/trading-system/data/*` (LIVE DBs). This repo's DBs:
  `data/paper-trade.db`, `data/equity-snapshots.db` (incl. `last_mark_prices`).
- Equity collector (`worker/app/equity_snapshots.py`) already writes
  `balance = capital + realized + unrealized` — it is the CORRECT reference; never regress it.
- `/live/*` pages on the dashboard are NOT this repo's data; they read `trading-system`.
- New alpha/strategy = skill `paper-trade-alpha-deploy`. TCBS token issues = skill `tcbs-key-rotation`.