ZIP_NAME   = paper-trade-system
ZIP_PATH   = /tmp/$(ZIP_NAME).zip
SERVER     = root@167.86.101.228
REMOTE_DIR = /root/paper-trade-system

# Read REGISTERED_ALPHAS from .env
ALPHAS = $(shell grep ^REGISTERED_ALPHAS .env | cut -d= -f2 | tr ',' ' ')

# Optional: specify single alpha, e.g. make alpha-up ALPHA=wilder
ALPHA ?= undefined

# ─── Core system (redis + worker + web) ──────────────────────────────────────

up:
	docker compose up -d

down:
	docker compose down --timeout 30

restart:
	docker compose restart

build:
	docker compose build

logs:
	docker compose logs -f

ps:
	docker compose ps

health:
	docker compose exec worker test -f /tmp/bot_health && echo "OK" || echo "UNHEALTHY"

clean:
	docker compose down --timeout 30 -v
	rm -rf data/paper-trade.db

shell:
	docker compose exec worker /bin/bash

# ─── Alpha management ────────────────────────────────────────────────────────

# Start all alphas listed in REGISTERED_ALPHAS
alphas-up:
	@for alpha in $(ALPHAS); do \
		echo "→ Starting $$alpha..."; \
		mkdir -p logs/alphas/$$alpha; \
		docker compose -f alphas/$$alpha/docker-compose.yml \
			-p $$alpha up -d --build; \
	done

# Stop all alphas listed in REGISTERED_ALPHAS
alphas-down:
	@for alpha in $(ALPHAS); do \
		echo "→ Stopping $$alpha..."; \
		docker compose -f alphas/$$alpha/docker-compose.yml \
			-p $$alpha down --timeout 30; \
	done

# Start a single alpha: make alpha-up ALPHA=wilder
alpha-up:
	@[ "$(ALPHA)" != "undefined" ] || (echo "Usage: make alpha-up ALPHA=<name>"; exit 1)
	mkdir -p logs/alphas/$(ALPHA)
	docker compose -f alphas/$(ALPHA)/docker-compose.yml -p $(ALPHA) up -d --build

# Stop a single alpha: make alpha-down ALPHA=wilder
alpha-down:
	@[ "$(ALPHA)" != "undefined" ] || (echo "Usage: make alpha-down ALPHA=<name>"; exit 1)
	docker compose -f alphas/$(ALPHA)/docker-compose.yml -p $(ALPHA) down --timeout 30

# Restart a single alpha
alpha-restart:
	@[ "$(ALPHA)" != "undefined" ] || (echo "Usage: make alpha-restart ALPHA=<name>"; exit 1)
	docker compose -f alphas/$(ALPHA)/docker-compose.yml -p $(ALPHA) restart

# Logs for a single alpha
alpha-logs:
	@[ "$(ALPHA)" != "undefined" ] || (echo "Usage: make alpha-logs ALPHA=<name>"; exit 1)
	docker compose -f alphas/$(ALPHA)/docker-compose.yml -p $(ALPHA) logs -f

# Status of all alphas
alphas-ps:
	@for alpha in $(ALPHAS); do \
		echo "=== $$alpha ==="; \
		docker compose -f alphas/$$alpha/docker-compose.yml -p $$alpha ps 2>/dev/null; \
	done

# ─── Package ─────────────────────────────────────────────────────────────────

package:
	cd .. && zip -r $(ZIP_PATH) $(ZIP_NAME)/ \
		-x "$(ZIP_NAME)/.git/*" \
		-x "$(ZIP_NAME)/**/__pycache__/*" \
		-x "$(ZIP_NAME)/**/.venv/*" \
		-x "$(ZIP_NAME)/**/node_modules/*" \
		-x "$(ZIP_NAME)/**/.next/*" \
		-x "$(ZIP_NAME)/data/paper-trade.db" \
		-x "$(ZIP_NAME)/data/paper-trade.db-shm" \
		-x "$(ZIP_NAME)/data/paper-trade.db-wal" \
		-x "$(ZIP_NAME)/logs/*" \
		-x "$(ZIP_NAME)/alphas/logs/*" \
		-x "$(ZIP_NAME)/graphify-out/*"
	@echo "Packaged → $(ZIP_PATH)"
	@zip -sf $(ZIP_PATH) | grep "\.env" && echo "✓ .env files included" || true

# ─── Deploy ──────────────────────────────────────────────────────────────────

deploy: package
	@echo "→ Uploading to $(SERVER)..."
	scp $(ZIP_PATH) $(SERVER):/tmp/
	ssh $(SERVER) '\
		set -e; \
		echo "[1/7] Extracting..."; \
		unzip -o /tmp/$(ZIP_NAME).zip -d /root/ > /dev/null; \
		cd $(REMOTE_DIR); \
		echo "[2/7] Backing up DB..."; \
		[ -f data/paper-trade.db ] && cp data/paper-trade.db data/paper-trade.db.bak || true; \
		echo "[3/7] Stopping alphas..."; \
		make alphas-down; \
		echo "[4/7] Stopping core..."; \
		docker compose down --timeout 30 -v; \
		echo "[5/7] Starting core..."; \
		docker compose up -d --build; \
		echo "[6/7] Checking DB health..."; \
		sleep 3; \
		if docker compose exec -T worker python3 -c \
			"import sqlite3; sqlite3.connect(\"/app/data/paper-trade.db\").execute(\"PRAGMA integrity_check\")" \
			2>/dev/null; then \
			echo "DB OK."; \
		else \
			echo "DB corrupt — restoring backup..."; \
			docker compose down --timeout 10; \
			mv data/paper-trade.db data/paper-trade.db.corrupt; \
			[ -f data/paper-trade.db.bak ] && cp data/paper-trade.db.bak data/paper-trade.db || true; \
			docker compose up -d; \
		fi; \
		echo "[7/7] Starting alphas..."; \
		make alphas-up; \
		WEB_PORT=$$(grep ^WEB_PORT .env | cut -d= -f2 | tr -d " #"); \
		echo "Dashboard → http://167.86.101.228:$$WEB_PORT"; \
	'

# Deploy + start all alphas
deploy-all: deploy
	ssh $(SERVER) 'cd $(REMOTE_DIR) && make alphas-up'

# Deploy a single alpha: make deploy-alpha ALPHA=wilder
ALPHA_ZIP = /tmp/alpha-$(ALPHA).zip

deploy-alpha:
	@[ "$(ALPHA)" != "undefined" ] || (echo "Usage: make deploy-alpha ALPHA=<name>"; exit 1)
	@[ -d alphas/$(ALPHA) ] || (echo "Alpha '$(ALPHA)' not found"; exit 1)
	@echo "→ Packaging alpha: $(ALPHA)..."
	cd .. && zip -r $(ALPHA_ZIP) \
		$(ZIP_NAME)/alphas/$(ALPHA)/ \
		$(ZIP_NAME)/base/ \
		$(ZIP_NAME)/.env \
		-x "$(ZIP_NAME)/**/__pycache__/*" \
		-x "$(ZIP_NAME)/**/.venv/*"
	scp $(ALPHA_ZIP) $(SERVER):/tmp/
	ssh $(SERVER) '\
		set -e; \
		echo "[1/3] Extracting..."; \
		unzip -o /tmp/alpha-$(ALPHA).zip -d /root/ > /dev/null; \
		mkdir -p $(REMOTE_DIR)/logs/alphas/$(ALPHA); \
		echo "[2/3] Building & starting $(ALPHA)..."; \
		cd $(REMOTE_DIR) && docker compose -f alphas/$(ALPHA)/docker-compose.yml \
			-p $(ALPHA) up -d --build; \
		echo "[3/3] Reloading worker..."; \
		docker compose up -d --force-recreate worker; \
		echo "Done. Alpha $(ALPHA) is live."; \
	'

deploy-restart:
	ssh $(SERVER) 'cd $(REMOTE_DIR) && docker compose restart'

deploy-logs:
	ssh $(SERVER) 'cd $(REMOTE_DIR) && docker compose logs -f'

deploy-ps:
	ssh $(SERVER) 'cd $(REMOTE_DIR) && docker compose ps && make alphas-ps'

deploy-prune:
	ssh $(SERVER) 'docker system prune -af --volumes'

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
