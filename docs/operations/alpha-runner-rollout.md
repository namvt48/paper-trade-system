# Alpha Runner Rollout

This rollout keeps old alpha containers live until shadow parity is proven.

## Phase 0: Preflight

Run from `paper-trade-system`.

```bash
make runner-build
PYTHONPATH=alphas python -m pytest alphas/runner/tests/ -q
PYTHONPATH=alphas python -m runner.main --config runner-config.shadow.yaml --dry-run
docker compose --profile runner --profile shadow config
```

Verify MDS, `paper-redis`, worker health, and old alpha logs before starting shadow mode.

## Phase 1: Shadow

```bash
make shadow-up
make runner-logs
make shadow-logs
```

Expected:

- old alpha containers continue writing to `paper-signals`,
- alpha runner writes only to `paper-signals-shadow`,
- shadow worker compares logical signal keys and reports match rate,
- no runner production signal appears while `shadow_mode: true`.

## Phase 2: Gradual Cutover

For one alpha group at a time:

1. Stop or deregister that group in the old alpha deployment.
2. Enable the same group in `runner-config.production.yaml`.
3. Start production runner config:

```bash
RUNNER_CONFIG_FILE=./runner-config.production.yaml make runner-up
make runner-logs
```

4. Verify leases are owned by the runner and production signals/positions look normal.
5. Continue to the next group only after the current group is stable.

Rollback:

1. Disable the group in runner config.
2. Stop the runner or wait for lease TTL to expire.
3. Restart the old alpha container for that group.

## Phase 3: Full Cutover

After all alpha groups pass:

- keep old alpha code available for a rollback window,
- archive shadow mismatch logs,
- remove old alpha containers only in a separate cleanup change.
