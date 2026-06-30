SHELL := /bin/sh

ZIP_NAME   := paper-trade-system
ZIP_PATH   := /tmp/$(ZIP_NAME).zip
WEB_ZIP_PATH := /tmp/$(ZIP_NAME)-web.zip
SERVER     ?= root@167.86.101.228
SERVER_HOST ?= $(shell printf '%s' '$(SERVER)' | sed 's/.*@//')
REMOTE_DIR ?= /root/paper-trade-system
COMPOSE    := docker compose
MDS_REDIS_URL ?= redis://mds-redis:6379
MDS_EXCHANGE  ?= binance

# Read REGISTERED_ALPHAS from .env
ALPHAS = $(shell [ -f .env ] && sed -n 's/^REGISTERED_ALPHAS=//p' .env | head -n1 | tr ',' ' ' || true)

# Optional: specify single alpha, e.g. make alpha-up ALPHA=wilder
ALPHA ?= undefined

.PHONY: help prepare up run down restart build logs logs-tail ps health clean shell \
	require-mds-network alphas-up alphas-down alpha-up alpha-down alpha-restart alpha-logs alphas-ps alphas-health \
	alpha-deregister \
	runner-build runner-up runner-down runner-scale runner-logs runner-health runner-sync-config runner-status runner-reconcile \
	test test-indicators test-cross-alpha test-runner \
	package deploy deploy-web deploy-core deploy-system deploy-all deploy-alpha deploy-legacy-runner deploy-alpha-deregister deploy-restart deploy-logs deploy-ps deploy-prune \
	deploy-db-reset deploy-db-recover db-trades db-summary db-open db-symbols db-csv db-alphas

help:
	@echo "Paper trade targets:"
	@echo "  make up              Build/start Redis + worker + web"
	@echo "  make runner-up       Build/start alpha-runner + alpha-runner-legacy"
	@echo "  make runner-build    Build runner images"
	@echo "  make runner-scale N=3 Scale alpha-runner replicas"
	@echo "  make runner-health   Check alpha-runner container; metrics is best-effort during warmup"
	@echo "  make runner-reconcile  Remove ghost Redis positions (run before runner-up after deploy-core)"
	@echo "  make alphas-down     Stop legacy standalone alpha containers, if any"
	@echo "  make alpha-deregister ALPHA=<name>          Stop + remove from DB and .env (local)"
	@echo "  make test            Run all tests (indicators + alphas)"
	@echo "  make test-indicators Run indicators library tests"
	@echo "  make test-cross-alpha Run cross-alpha strategy tests"
	@echo "  make test-runner     Run alpha-runner tests"
	@echo "  make health          Check core, stream, DB, web, and runner"
	@echo "  make package         Build deploy zip at $(ZIP_PATH)"
	@echo "  make deploy          Upload, start core + runner on SERVER=$(SERVER)"
	@echo "  make deploy-legacy-runner Upload/start only alpha-runner-legacy; leave core + existing runner running"
	@echo "  make deploy-web      Upload and recreate only web; leave Redis, worker, and runner running"
	@echo "  make deploy-core     Upload and start only Redis + worker + web; do not start alphas"
	@echo "  make deploy-alpha-deregister ALPHA=<name>   Stop + remove from DB and .env on SERVER"
	@echo "  make deploy-logs     Follow remote core logs"
	@echo "  make deploy-ps       Remote core + alpha status"

# ─── Core system (redis + worker + web) ──────────────────────────────────────

prepare:
	mkdir -p data logs/redis logs/worker logs/web logs/alphas logs/runner

up run: prepare
	$(COMPOSE) up -d --build --remove-orphans

down:
	$(COMPOSE) down --timeout 30

restart:
	$(COMPOSE) restart

build:
	$(COMPOSE) build

logs:
	$(COMPOSE) logs -f

logs-tail:
	$(COMPOSE) logs --tail=200

ps:
	$(COMPOSE) ps

health:
	$(COMPOSE) ps
	@$(COMPOSE) exec -T worker python -c "import redis; r=redis.Redis.from_url('redis://paper-redis:6379'); print('paper-redis', r.ping())"
	@$(COMPOSE) exec -T worker test -f /tmp/bot_health && echo "worker OK" || (echo "worker UNHEALTHY"; exit 1)
	@$(COMPOSE) exec -T worker python -c "import redis; r=redis.Redis.from_url('redis://paper-redis:6379', decode_responses=True); print('paper-signals groups', r.xinfo_groups('paper-signals') if r.exists('paper-signals') else [])" || true
	@$(COMPOSE) exec -T worker python -c "import sqlite3; con=sqlite3.connect('/app/data/paper-trade.db'); print('db', con.execute('PRAGMA integrity_check').fetchone()[0]); print('alphas', con.execute('select alpha_id,status from alphas order by alpha_id').fetchall())"
	@$(COMPOSE) exec -T web node -e "fetch('http://127.0.0.1:3000/api/dashboard').then(r=>{if(!r.ok) throw new Error(r.status); return r.json()}).then(j=>console.log('web OK', j.alphas.length, 'alphas')).catch(e=>{console.error(e); process.exit(1)})"
	@$(MAKE) --no-print-directory runner-health

clean:
	$(COMPOSE) down --timeout 30 -v
	rm -rf data/paper-trade.db

shell:
	$(COMPOSE) exec worker /bin/bash

# ─── Alpha runner / shadow rollout ───────────────────────────────────────────

runner-build:
	$(COMPOSE) --profile runner build alpha-runner alpha-runner-legacy

runner-up: prepare
	$(COMPOSE) --profile runner up -d --build alpha-runner alpha-runner-legacy

runner-down:
	$(COMPOSE) --profile runner stop alpha-runner alpha-runner-legacy
	$(COMPOSE) --profile runner rm -f alpha-runner alpha-runner-legacy

runner-scale: prepare
	@[ -n "$(N)" ] || (echo "Usage: make runner-scale N=<replicas>"; exit 1)
	$(COMPOSE) --profile runner up -d --build --scale alpha-runner=$(N) alpha-runner

runner-logs:
	$(COMPOSE) --profile runner logs -f alpha-runner alpha-runner-legacy

runner-health:
	$(COMPOSE) --profile runner ps alpha-runner alpha-runner-legacy
	@$(COMPOSE) --profile runner exec -T alpha-runner true
	@$(COMPOSE) --profile runner exec -T alpha-runner python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.getenv('RUNNER_METRICS_PORT','9091'), timeout=3).read(); print('alpha-runner metrics OK')" 2>/dev/null || echo "alpha-runner running; metrics not ready yet"
	@$(COMPOSE) --profile runner exec -T alpha-runner-legacy true
	@$(COMPOSE) --profile runner exec -T alpha-runner-legacy python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.getenv('RUNNER_METRICS_PORT','9092'), timeout=3).read(); print('alpha-runner-legacy metrics OK')" 2>/dev/null || echo "alpha-runner-legacy running; metrics not ready yet"

# --- Multi-runner targets ---
runner-sync-config: ## Sync runner-config.yaml → Redis
	docker compose --profile runner run --rm alpha-runner python -m runner.config_sync --config /config/runner-config.yaml --redis-url redis://paper-redis:6379
	docker compose --profile runner run --rm alpha-runner-legacy python -m runner.config_sync --config /config/runner-config.yaml --redis-url redis://paper-redis:6379 --no-prune

runner-status: ## Show alpha → runner assignment map
	docker compose --profile runner run --rm alpha-runner python -m runner.status

runner-reconcile: ## Remove ghost Redis positions (runner:positions:* keys with no DB positions)
	@echo "→ Reconciling Redis positions with DB..."
	@docker compose exec -T worker python3 -c "import sqlite3,redis; con=sqlite3.connect('/app/data/paper-trade.db'); db={r[0] for r in con.execute('SELECT DISTINCT alpha_id FROM positions')}; c=redis.from_url('redis://paper-redis:6379',decode_responses=True); removed=sum(c.delete(k) for k in c.scan_iter('runner:positions:*') if k.replace('runner:positions:','') not in db); print(f'Reconciled: removed {removed} ghost position keys')"

# ─── Alpha management ────────────────────────────────────────────────────────

require-mds-network:
	@docker network inspect redis-net >/dev/null 2>&1 || (echo "Missing Docker network 'redis-net'. Deploy/start infra/redis first."; exit 1)

# Legacy standalone alpha containers are no longer started directly. Alphas run
# inside alpha-runner from runner-config.yaml / Redis config.
alphas-up: require-mds-network
	@echo "ERROR: legacy standalone alpha startup is disabled. Use 'make runner-up' or 'make runner-scale N=<replicas>'."; exit 1

# Stop all alphas listed in REGISTERED_ALPHAS
alphas-down:
	@for alpha in $(ALPHAS); do \
		echo "→ Stopping $$alpha..."; \
		$(COMPOSE) -f alphas/$$alpha/docker-compose.yml \
			-p $$alpha down --timeout 30; \
	done

# Legacy standalone alpha startup is disabled. Add/enable alpha in runner-config.yaml instead.
alpha-up: require-mds-network
	@echo "ERROR: legacy standalone alpha startup is disabled. Use runner config + 'make runner-up'."; exit 1

# Stop a single alpha: make alpha-down ALPHA=wilder
alpha-down:
	@[ "$(ALPHA)" != "undefined" ] || (echo "Usage: make alpha-down ALPHA=<name>"; exit 1)
	$(COMPOSE) -f alphas/$(ALPHA)/docker-compose.yml -p $(ALPHA) down --timeout 30

# Fully stop an alpha and remove it from REGISTERED_ALPHAS + DB.
# Deletes open positions, the registry row, and alpha_columns. Trade history is kept.
# Usage: make alpha-deregister ALPHA=wilder
alpha-deregister:
	@[ "$(ALPHA)" != "undefined" ] || (echo "Usage: make alpha-deregister ALPHA=<name>"; exit 1)
	@echo "→ Stopping $(ALPHA)..."
	$(COMPOSE) -f alphas/$(ALPHA)/docker-compose.yml -p $(ALPHA) down --timeout 30 || true
	@echo "→ Removing $(ALPHA) from REGISTERED_ALPHAS..."
	@sed -i 's/,$(ALPHA)//' .env
	@sed -i 's/$(ALPHA),//' .env
	@sed -i 's/^\(REGISTERED_ALPHAS=\)$(ALPHA)$$/\1/' .env
	@echo "→ Removing $(ALPHA) from database..."
	@$(COMPOSE) exec -T worker python3 -c "import sqlite3; con=sqlite3.connect('/app/data/paper-trade.db'); n=con.execute('SELECT COUNT(*) FROM positions WHERE alpha_id=?',('$(ALPHA)',)).fetchone()[0]; n and print(f'  removing {n} open position(s)'); [con.execute(s,('$(ALPHA)',)) for s in ['DELETE FROM positions WHERE alpha_id=?','DELETE FROM alphas WHERE alpha_id=?','DELETE FROM alpha_columns WHERE alpha_id=?']]; con.commit(); print('  DB updated')"
	@echo "$(ALPHA) stopped and deregistered."

# Restart a single alpha
alpha-restart:
	@[ "$(ALPHA)" != "undefined" ] || (echo "Usage: make alpha-restart ALPHA=<name>"; exit 1)
	@echo "ERROR: legacy standalone alpha restart is disabled. Use 'make runner-down && make runner-up' or update runner-config.yaml."; exit 1

# Logs for a single alpha
alpha-logs:
	@[ "$(ALPHA)" != "undefined" ] || (echo "Usage: make alpha-logs ALPHA=<name>"; exit 1)
	@echo "ERROR: legacy standalone alpha logs are disabled. Use logs/runner/alphas/$(ALPHA).log or 'make runner-logs'."; exit 1

# Status of all alphas
alphas-ps:
	@echo "ERROR: legacy standalone alpha status is disabled. Use 'make runner-status' or 'make runner-health'."; exit 1

alphas-health:
	@echo "ERROR: legacy standalone alpha health is disabled. Use 'make runner-health'."; exit 1

test:
	PYTHONPATH=.:alphas python -m pytest indicators/ alphas/ -v

test-indicators:
	python -m pytest indicators/ -v

test-cross-alpha:
	PYTHONPATH=.:alphas python -m pytest alphas/cross_alpha/ -v

test-runner:
	PYTHONPATH=.:alphas python -m pytest alphas/runner/ -v

# ─── Package ─────────────────────────────────────────────────────────────────

package:
	rm -f $(ZIP_PATH)
	cd .. && zip -r $(ZIP_PATH) $(ZIP_NAME)/ \
		-x "$(ZIP_NAME)/.git/*" \
		-x "$(ZIP_NAME)/.pytest_cache/*" \
		-x "$(ZIP_NAME)/.codegraph/*" \
		-x "$(ZIP_NAME)/**/__pycache__/*" \
		-x "$(ZIP_NAME)/**/.venv/*" \
		-x "$(ZIP_NAME)/**/.pytest_cache/*" \
		-x "$(ZIP_NAME)/**/.mypy_cache/*" \
		-x "$(ZIP_NAME)/**/.ruff_cache/*" \
		-x "$(ZIP_NAME)/**/node_modules/*" \
		-x "$(ZIP_NAME)/**/.next/*" \
		-x "$(ZIP_NAME)/data/paper-trade.db" \
		-x "$(ZIP_NAME)/data/paper-trade.db-shm" \
		-x "$(ZIP_NAME)/data/paper-trade.db-wal" \
		-x "$(ZIP_NAME)/logs/*" \
		-x "$(ZIP_NAME)/benchmarks/results/*" \
		-x "$(ZIP_NAME)/graphify-out/*" \
		-x "$(ZIP_NAME)/*.log" \
		-x "$(ZIP_NAME)/**/*.log"
	@echo "Packaged → $(ZIP_PATH)"
	@zip -sf $(ZIP_PATH) | grep "\.env" && echo "✓ .env files included" || true

# ─── Deploy ──────────────────────────────────────────────────────────────────

deploy-web:
	@echo "→ Packaging web..."
	rm -f $(WEB_ZIP_PATH)
	cd .. && zip -r $(WEB_ZIP_PATH) \
		$(ZIP_NAME)/web/ \
		$(ZIP_NAME)/docker-compose.yml \
		$(ZIP_NAME)/.env \
		-x "$(ZIP_NAME)/web/node_modules/*" \
		-x "$(ZIP_NAME)/web/.next/*" \
		-x "$(ZIP_NAME)/web/tsconfig.tsbuildinfo"
	@echo "→ Uploading web to $(SERVER)..."
	scp $(WEB_ZIP_PATH) $(SERVER):/tmp/
	ssh $(SERVER) '\
		set -e; \
		echo "[1/3] Extracting web..."; \
		mkdir -p $(REMOTE_DIR); \
		unzip -o $(WEB_ZIP_PATH) -d /root/ > /dev/null; \
		cd $(REMOTE_DIR); \
		mkdir -p data logs/web; \
		echo "[2/3] Building and recreating web only..."; \
		docker compose up -d --build --no-deps web; \
		echo "[3/3] Checking web..."; \
		i=0; \
		until docker compose exec -T web node -e \
			"fetch(\"http://127.0.0.1:3000/api/dashboard\").then(r=>{if(!r.ok) throw new Error(r.status); return r.json()}).then(j=>console.log(\"web OK\", j.alphas.length, \"alphas\")).catch(e=>{console.error(e); process.exit(1)})"; do \
			i=$$((i+1)); \
			[ $$i -lt 30 ] || (docker compose logs --tail=120 web; exit 1); \
			sleep 2; \
		done; \
		docker compose ps web; \
		WEB_PORT=$$(grep ^WEB_PORT .env | cut -d= -f2 | tr -d " #"); \
		echo "Dashboard → http://$(SERVER_HOST):$$WEB_PORT"; \
	'

deploy: package
	@echo "→ Uploading to $(SERVER)..."
	scp $(ZIP_PATH) $(SERVER):/tmp/
	ssh $(SERVER) '\
		set -e; \
		echo "[1/10] Extracting..."; \
		mkdir -p $(REMOTE_DIR); \
		unzip -o /tmp/$(ZIP_NAME).zip -d /root/ > /dev/null; \
		cd $(REMOTE_DIR); \
		echo "[2/10] Preparing runtime dirs..."; \
		make prepare; \
		echo "[3/10] Backing up DB..."; \
		[ -f data/paper-trade.db ] && cp data/paper-trade.db data/paper-trade.db.bak || true; \
		echo "[4/10] Stopping legacy standalone alphas and old runner..."; \
		make alphas-down || true; \
		make runner-down || true; \
		echo "[5/10] Clearing old logs..."; \
		find logs -type f -delete 2>/dev/null || true; \
		echo "[6/10] Starting core..."; \
		docker compose up -d --build --remove-orphans; \
		echo "[7/10] Checking core health..."; \
		i=0; \
		until docker compose exec -T worker test -f /tmp/bot_health >/dev/null 2>&1; do \
			i=$$((i+1)); \
			[ $$i -lt 30 ] || (docker compose logs --tail=120 worker; exit 1); \
			sleep 2; \
		done; \
		if docker compose exec -T worker python3 -c \
			"import sqlite3; con=sqlite3.connect(\"/app/data/paper-trade.db\"); assert con.execute(\"PRAGMA integrity_check\").fetchone()[0] == \"ok\"" \
			2>/dev/null; then \
			echo "DB OK."; \
		else \
			echo "DB corrupt — restoring backup..."; \
			docker compose down --timeout 10; \
			mv data/paper-trade.db data/paper-trade.db.corrupt; \
			[ -f data/paper-trade.db.bak ] && cp data/paper-trade.db.bak data/paper-trade.db || true; \
			docker compose up -d --build; \
		fi; \
		docker compose exec -T web node -e \
			"fetch(\"http://127.0.0.1:3000/api/dashboard\").then(r=>{if(!r.ok) throw new Error(r.status); return r.json()}).then(j=>console.log(\"web OK\", j.alphas.length, \"alphas\")).catch(e=>{console.error(e); process.exit(1)})"; \
		echo "[8/10] Reconciling Redis positions with DB..."; \
		docker compose exec -T worker python3 -c "import sqlite3,redis; con=sqlite3.connect(\"/app/data/paper-trade.db\"); db={r[0] for r in con.execute(\"SELECT DISTINCT alpha_id FROM positions\")}; c=redis.from_url(\"redis://paper-redis:6379\",decode_responses=True); removed=sum(c.delete(k) for k in c.scan_iter(\"runner:positions:*\") if k.replace(\"runner:positions:\",\"\") not in db); print(f\"Reconciled: removed {removed} ghost position keys\")" || echo "Reconcile skipped (worker not ready)"; \
		echo "[9/10] Starting alpha-runner..."; \
		make runner-build; \
		make runner-sync-config; \
		make runner-up; \
		docker compose --profile runner exec -T alpha-runner true; \
		make runner-health; \
		echo "[10/10] Status"; \
		make health; \
		docker compose --profile runner logs --tail=80 alpha-runner alpha-runner-legacy; \
		WEB_PORT=$$(grep ^WEB_PORT .env | cut -d= -f2 | tr -d " #"); \
		echo "Dashboard → http://$(SERVER_HOST):$$WEB_PORT"; \
	'

# Deploy only the core system (redis + worker + web). This intentionally stops
# configured alphas and does not start them again.
deploy-core deploy-system: package
	@echo "→ Uploading to $(SERVER)..."
	scp $(ZIP_PATH) $(SERVER):/tmp/
	ssh $(SERVER) '\
		set -e; \
		echo "[1/7] Extracting..."; \
		mkdir -p $(REMOTE_DIR); \
		unzip -o /tmp/$(ZIP_NAME).zip -d /root/ > /dev/null; \
		cd $(REMOTE_DIR); \
		echo "[2/7] Preparing runtime dirs..."; \
		make prepare; \
		echo "[3/7] Backing up DB..."; \
		[ -f data/paper-trade.db ] && cp data/paper-trade.db data/paper-trade.db.bak || true; \
		echo "[4/7] Stopping alphas..."; \
		make alphas-down || true; \
		echo "[5/7] Starting core only..."; \
		docker compose up -d --build --remove-orphans; \
		echo "[6/7] Checking core health..."; \
		i=0; \
		until docker compose exec -T worker test -f /tmp/bot_health >/dev/null 2>&1; do \
			i=$$((i+1)); \
			[ $$i -lt 30 ] || (docker compose logs --tail=120 worker; exit 1); \
			sleep 2; \
		done; \
		docker compose exec -T worker python3 -c \
			"import sqlite3; con=sqlite3.connect(\"/app/data/paper-trade.db\"); assert con.execute(\"PRAGMA integrity_check\").fetchone()[0] == \"ok\"; print(\"DB OK\")"; \
		docker compose exec -T web node -e \
			"fetch(\"http://127.0.0.1:3000/api/dashboard\").then(r=>{if(!r.ok) throw new Error(r.status); return r.json()}).then(j=>console.log(\"web OK\", j.alphas.length, \"alphas\")).catch(e=>{console.error(e); process.exit(1)})"; \
		echo "[7/7] Core status"; \
		docker compose ps; \
		WEB_PORT=$$(grep ^WEB_PORT .env | cut -d= -f2 | tr -d " #"); \
		echo "Dashboard → http://$(SERVER_HOST):$$WEB_PORT"; \
		echo "Legacy standalone alphas are stopped. Start runner later with: make runner-up"; \
	'

# Deploy + start core and alpha-runner. Kept as an alias for older muscle memory.
deploy-all: deploy

# Deploy a single alpha: make deploy-alpha ALPHA=wilder
ALPHA_ZIP = /tmp/alpha-$(ALPHA).zip

deploy-alpha:
	@[ "$(ALPHA)" != "undefined" ] || (echo "Usage: make deploy-alpha ALPHA=<name>"; exit 1)
	@echo "ERROR: legacy standalone alpha deploy is disabled. Update runner-config.yaml and run 'make deploy' or 'make runner-up'."; exit 1

deploy-legacy-runner: package
	@echo "→ Uploading legacy runner update to $(SERVER)..."
	scp $(ZIP_PATH) $(SERVER):/tmp/
	ssh $(SERVER) '\
		set -e; \
		echo "[1/6] Extracting files only..."; \
		mkdir -p $(REMOTE_DIR); \
		unzip -o /tmp/$(ZIP_NAME).zip -d /root/ > /dev/null; \
		cd $(REMOTE_DIR); \
		echo "[2/6] Preparing dirs..."; \
		make prepare; \
		mkdir -p logs/runner-legacy logs/runner-legacy/alphas; \
		echo "[3/6] Registering 7 legacy alpha rows in DB (no worker restart)..."; \
		docker compose exec -T worker python3 -c "import sqlite3,datetime; alphas=\"alpha-1-bangoc,alpha-1-v5b,alpha-1-v5b-2-8pct-reverse-blacklist-reverse-2-8pct,alpha-1-v5b-reverse-blacklist-base-reverse,alpha-2,hyper-turbo,hyper-turbo-v2\".split(\",\"); con=sqlite3.connect(\"/app/data/paper-trade.db\"); now=datetime.datetime.now(datetime.UTC).isoformat(); [con.execute(\"INSERT OR IGNORE INTO alphas (alpha_id, display_name, created_at, status) VALUES (?, ?, ?, ?)\", (a, a, now, \"active\")) for a in alphas]; con.commit(); print(\"registered\", len(alphas), \"legacy alphas\")"; \
		echo "[4/6] Building alpha-runner-legacy only..."; \
		docker compose --profile runner build alpha-runner-legacy; \
		echo "[5/6] Starting alpha-runner-legacy only (no deps, no Redis config sync)..."; \
		docker compose --profile runner up -d --no-deps alpha-runner-legacy; \
		echo "[6/6] Status"; \
		docker compose --profile runner ps alpha-runner alpha-runner-legacy; \
		docker compose --profile runner logs --tail=80 alpha-runner-legacy; \
	'

# Stop a single alpha on the server and remove it from REGISTERED_ALPHAS + DB.
# Deletes open positions, the registry row, and alpha_columns. Trade history is kept.
# Usage: make deploy-alpha-deregister ALPHA=wilder
deploy-alpha-deregister:
	@[ "$(ALPHA)" != "undefined" ] || (echo "Usage: make deploy-alpha-deregister ALPHA=<name>"; exit 1)
	@echo "→ Deregistering $(ALPHA) on $(SERVER)..."
	ssh $(SERVER) '\
		set -e; cd $(REMOTE_DIR); \
		echo "[1/3] Stopping $(ALPHA)..."; \
		docker compose -f alphas/$(ALPHA)/docker-compose.yml -p $(ALPHA) down --timeout 30 || true; \
		echo "[2/3] Removing from REGISTERED_ALPHAS..."; \
		sed -i "s/,$(ALPHA)//" .env; \
		sed -i "s/$(ALPHA),//" .env; \
		sed -i "s/^\(REGISTERED_ALPHAS=\)$(ALPHA)$$/\1/" .env; \
		echo "[3/3] Removing from DB..."; \
		docker compose exec -T worker python3 -c "import sqlite3; alpha=\"$(ALPHA)\"; con=sqlite3.connect(\"/app/data/paper-trade.db\"); n=con.execute(\"SELECT COUNT(*) FROM positions WHERE alpha_id=?\",(alpha,)).fetchone()[0]; n and print(f\"  removing {n} open position(s)\"); [con.execute(f\"DELETE FROM {t} WHERE alpha_id=?\",(alpha,)) for t in [\"positions\",\"alphas\",\"alpha_columns\"]]; con.commit(); print(\"  DB updated\")"; \
		echo "$(ALPHA) fully deregistered."; \
	'

deploy-restart:
	ssh $(SERVER) 'cd $(REMOTE_DIR) && docker compose restart'

deploy-logs:
	ssh $(SERVER) 'cd $(REMOTE_DIR) && docker compose logs -f'

deploy-ps:
	ssh $(SERVER) 'cd $(REMOTE_DIR) && docker compose --profile runner ps && make runner-status || true'

deploy-prune:
	ssh $(SERVER) 'docker system prune -af'

# ─── Trade history ───────────────────────────────────────────────────────────
#  Usage:
#    make db-trades   ALPHA=wilder            # recent trades (default 50)
#    make db-trades   ALPHA=wilder LIMIT=200  # more rows
#    make db-summary  ALPHA=wilder            # win rate, total PnL, etc.
#    make db-open     ALPHA=wilder            # open positions
#    make db-symbols  ALPHA=wilder            # PnL breakdown by symbol
#    make db-csv      ALPHA=wilder            # export all trades to CSV
#    make db-alphas                           # list all registered alphas

DB     := data/paper-trade.db
LIMIT  ?= 50

# Use system sqlite3 if available, otherwise spin up a Docker container
SQLITE3 := $(shell which sqlite3 2>/dev/null)
ifdef SQLITE3
  _sq = sqlite3
else
  _sq = docker run --rm -v "$(CURDIR)/$(DB):/db.sqlite:ro" nouchka/sqlite3 /db.sqlite
endif

db-trades:
	@[ "$(ALPHA)" != "undefined" ] || (echo "Usage: make db-trades ALPHA=<name>"; exit 1)
	@echo "=== Trades: $(ALPHA) (last $(LIMIT)) ==="
	@$(_sq) -column -header \
	"SELECT \
	    substr(closed_at,1,16)         AS closed, \
	    symbol, side, \
	    printf('%.4f', entry_price)    AS entry, \
	    printf('%.4f', exit_price)     AS exit, \
	    printf('%+.2f', pnl)           AS pnl, \
	    printf('%+.2f%%', pnl_percent) AS pnl_pct, \
	    printf('%.1fh', duration_hours) AS dur, \
	    reason \
	FROM trades \
	WHERE alpha_id = '$(ALPHA)' \
	ORDER BY closed_at DESC LIMIT $(LIMIT);"

db-summary:
	@[ "$(ALPHA)" != "undefined" ] || (echo "Usage: make db-summary ALPHA=<name>"; exit 1)
	@echo "=== Summary: $(ALPHA) ==="
	@$(_sq) -column \
	"SELECT \
	    COUNT(*)                                                                       AS total, \
	    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)                                     AS wins, \
	    SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END)                                    AS losses, \
	    printf('%.1f%%', 100.0*SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)/MAX(COUNT(*),1)) AS win_rate, \
	    printf('%+.2f', SUM(pnl))                                                    AS total_pnl, \
	    printf('%+.2f', AVG(pnl))                                                    AS avg_pnl, \
	    printf('%+.2f', MAX(pnl))                                                    AS best, \
	    printf('%+.2f', MIN(pnl))                                                    AS worst, \
	    printf('%.1fh', AVG(duration_hours))                                         AS avg_dur \
	FROM trades WHERE alpha_id = '$(ALPHA)';"

db-open:
	@[ "$(ALPHA)" != "undefined" ] || (echo "Usage: make db-open ALPHA=<name>"; exit 1)
	@echo "=== Open positions: $(ALPHA) ==="
	@$(_sq) -column -header \
	"SELECT symbol, side, \
	    printf('%.4f', entry_price) AS entry, \
	    printf('%.4f', tp)          AS tp, \
	    printf('%.4f', sl)          AS sl, \
	    leverage, substr(opened_at,1,16) AS opened \
	FROM positions \
	WHERE alpha_id = '$(ALPHA)' ORDER BY opened_at DESC;"

db-symbols:
	@[ "$(ALPHA)" != "undefined" ] || (echo "Usage: make db-symbols ALPHA=<name>"; exit 1)
	@echo "=== By symbol: $(ALPHA) ==="
	@$(_sq) -column -header \
	"SELECT symbol, COUNT(*) AS trades, \
	    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins, \
	    printf('%.1f%%', 100.0*SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)/MAX(COUNT(*),1)) AS win_rate, \
	    printf('%+.2f', SUM(pnl)) AS total_pnl, \
	    printf('%+.2f', AVG(pnl)) AS avg_pnl \
	FROM trades WHERE alpha_id = '$(ALPHA)' \
	GROUP BY symbol ORDER BY SUM(pnl) DESC;"

db-csv:
	@[ "$(ALPHA)" != "undefined" ] || (echo "Usage: make db-csv ALPHA=<name>"; exit 1)
	@$(_sq) -csv -header \
	    "SELECT * FROM trades WHERE alpha_id='$(ALPHA)' ORDER BY closed_at DESC;" \
	    > $(ALPHA)-trades.csv
	@echo "→ Saved: $(ALPHA)-trades.csv"

db-alphas:
	@echo "=== Registered alphas ==="
	@$(_sq) -column -header \
	"SELECT a.alpha_id, a.status, COUNT(t.trade_id) AS trades, \
	    printf('%+.2f', COALESCE(SUM(t.pnl),0)) AS total_pnl, \
	    substr(a.created_at,1,16) AS created \
	FROM alphas a LEFT JOIN trades t ON t.alpha_id = a.alpha_id \
	GROUP BY a.alpha_id ORDER BY a.created_at;"

# ─── DB Recovery ─────────────────────────────────────────────────────────────

deploy-db-reset:
	@echo "⚠ This will delete all trade history on the server."
	@read -p "Continue? [y/N] " c; [ "$$c" = "y" ] || exit 1
	ssh $(SERVER) '\
		cd $(REMOTE_DIR); \
		docker compose down --timeout 30; \
		rm -f data/paper-trade.db; \
		docker compose up -d; \
		echo "DB reset."; \
	'

deploy-db-recover:
	ssh $(SERVER) '\
		cd $(REMOTE_DIR); \
		docker compose down --timeout 30; \
		if sqlite3 data/paper-trade.db ".dump" > /tmp/db-dump.sql 2>/dev/null; then \
			mv data/paper-trade.db data/paper-trade.db.bak; \
			sqlite3 data/paper-trade.db < /tmp/db-dump.sql; \
			echo "Recovered. Backup at data/paper-trade.db.bak"; \
		else \
			echo "Dump failed — falling back to reset."; \
			mv data/paper-trade.db data/paper-trade.db.bak; \
		fi; \
		docker compose up -d; \
	'
