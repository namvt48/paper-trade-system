import Database from "better-sqlite3";
import fs from "fs";
import path from "path";
import type { Alpha, Position, Trade, EquityPoint, AlphaStats, AlphaConfig, ColumnSpec } from "./types";
import { createLogger } from "./logger";

const log = createLogger("db");

const DB_PATH = process.env.DB_PATH || path.join(process.cwd(), "data", "paper-trade.db");
const ALPHAS_DIR = process.env.ALPHAS_DIR || path.join(process.cwd(), "alphas");
const DASHBOARD_CACHE_MS = Number(process.env.DASHBOARD_CACHE_MS || "1000");
const EQUITY_MAX_POINTS = Number(process.env.EQUITY_MAX_POINTS || "1000");
const configuredTradeHistoryLimit = Number(process.env.TRADE_HISTORY_LIMIT || "500");
export const TRADE_HISTORY_LIMIT = Number.isInteger(configuredTradeHistoryLimit) && configuredTradeHistoryLimit > 0
  ? configuredTradeHistoryLimit
  : 500;

declare global {
  // eslint-disable-next-line no-var
  var __paperTradeDb: Database.Database | undefined;
}

let alphaConfigCache: { mtime: number; value: Record<string, AlphaConfig> } | null = null;
let dashboardCache: { expiresAt: number; value: ReturnType<typeof buildDashboardData> } | null = null;
const statsCache = new Map<string, { expiresAt: number; value: AlphaStats }>();
const STATS_CACHE_MS = Number(process.env.STATS_CACHE_MS || "2000");

function parseDotEnv(content: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const line of content.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;
    const key = trimmed.slice(0, eq).trim();
    const val = trimmed.slice(eq + 1).split("#")[0].trim();
    result[key] = val;
  }
  return result;
}

function parseAlphaConfigs(): Record<string, AlphaConfig> {
  let mtime = 0;
  try {
    const entries = fs.readdirSync(ALPHAS_DIR, { withFileTypes: true });
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const envPath = path.join(ALPHAS_DIR, entry.name, ".env");
      if (!fs.existsSync(envPath)) continue;
      mtime += fs.statSync(envPath).mtimeMs;
    }
    if (alphaConfigCache && alphaConfigCache.mtime === mtime) {
      return alphaConfigCache.value;
    }

    const configs: Record<string, AlphaConfig> = {};
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const envPath = path.join(ALPHAS_DIR, entry.name, ".env");
      if (!fs.existsSync(envPath)) continue;
      const raw = fs.readFileSync(envPath, "utf-8");
      const parsed = parseDotEnv(raw);
      const alphaId = parsed["ALPHA_ID"] || entry.name;
      configs[alphaId] = parsed as AlphaConfig;
    }
    alphaConfigCache = { mtime, value: configs };
    return configs;
  } catch {
    return {};
  }
}

export function getAlphaConfig(alphaId: string): AlphaConfig {
  return parseAlphaConfigs()[alphaId] || {};
}

function getDb(): Database.Database {
  if (!globalThis.__paperTradeDb) {
    globalThis.__paperTradeDb = new Database(DB_PATH, { readonly: true, fileMustExist: true });
    globalThis.__paperTradeDb.pragma("query_only = ON");
    globalThis.__paperTradeDb.pragma("busy_timeout = 5000");
  }
  return globalThis.__paperTradeDb;
}

function tryGetDb(): Database.Database | null {
  try {
    return getDb();
  } catch (err) {
    log.error("failed to open database", { path: DB_PATH, err: String(err) });
    return null;
  }
}

export function getAllAlphas(): Alpha[] {
  const db = tryGetDb();
  if (!db) return [];
  return db.prepare("SELECT * FROM alphas ORDER BY created_at").all() as Alpha[];
}

export function getAlpha(alphaId: string): Alpha | undefined {
  const db = tryGetDb();
  if (!db) return undefined;
  return db.prepare("SELECT * FROM alphas WHERE alpha_id = ?").get(alphaId) as Alpha | undefined;
}

export function getOpenPositions(alphaId?: string): Position[] {
  const db = tryGetDb();
  if (!db) return [];
  if (alphaId) {
    return db.prepare("SELECT * FROM positions WHERE alpha_id = ? ORDER BY opened_at DESC").all(alphaId) as Position[];
  }
  return db.prepare("SELECT * FROM positions ORDER BY opened_at DESC").all() as Position[];
}

export function getTrades(alphaId: string, limit = 100, offset = 0): Trade[] {
  const db = tryGetDb();
  if (!db) return [];
  return db.prepare("SELECT * FROM trades WHERE alpha_id = ? ORDER BY closed_at DESC LIMIT ? OFFSET ?").all(alphaId, limit, offset) as Trade[];
}

export function getAllTrades(alphaId: string): Trade[] {
  const db = tryGetDb();
  if (!db) return [];
  return db.prepare("SELECT * FROM trades WHERE alpha_id = ? ORDER BY closed_at DESC").all(alphaId) as Trade[];
}

function downsampleEquity(rows: EquityPoint[], maxPoints: number): EquityPoint[] {
  if (maxPoints <= 0 || rows.length <= maxPoints) return rows;
  const sampled: EquityPoint[] = [];
  const step = (rows.length - 1) / (maxPoints - 1);
  for (let i = 0; i < maxPoints; i += 1) {
    sampled.push(rows[Math.round(i * step)]);
  }
  return sampled;
}

export function getEquityCurve(alphaId: string, maxPoints = EQUITY_MAX_POINTS): EquityPoint[] {
  const db = tryGetDb();
  if (!db) return [];
  const rows = db.prepare(`
    SELECT closed_at, SUM(pnl) OVER (ORDER BY closed_at) as equity
    FROM trades WHERE alpha_id = ?
    ORDER BY closed_at
  `).all(alphaId) as EquityPoint[];
  return downsampleEquity(rows, maxPoints);
}

export function getCompareEquity(alphaIds: string[]): Map<string, EquityPoint[]> {
  const result = new Map<string, EquityPoint[]>();
  for (const id of alphaIds) {
    result.set(id, getEquityCurve(id));
  }
  return result;
}

export function getAlphaStats(alphaId: string): AlphaStats {
  const now = Date.now();
  const cached = statsCache.get(alphaId);
  if (cached && cached.expiresAt > now) return cached.value;

  const db = tryGetDb();
  if (!db) return { alpha_id: alphaId, total_trades: 0, win_trades: 0, loss_trades: 0, winrate: 0, total_pnl: 0, avg_pnl: 0, avg_win: 0, avg_loss: 0, max_drawdown: 0, sharpe_ratio: 0, consecutive_wins: 0, consecutive_losses: 0 };
  const row = db.prepare(`
      SELECT
        ? as alpha_id,
        COUNT(*) as total_trades,
        SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as win_trades,
        SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as loss_trades,
        ROUND(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) as winrate,
        ROUND(SUM(pnl), 4) as total_pnl,
        ROUND(AVG(pnl), 4) as avg_pnl,
        ROUND(AVG(CASE WHEN pnl > 0 THEN pnl END), 4) as avg_win,
        ROUND(AVG(CASE WHEN pnl <= 0 THEN pnl END), 4) as avg_loss
      FROM trades WHERE alpha_id = ?
  `).get(alphaId, alphaId) as any;

  const pnlList = db.prepare("SELECT pnl FROM trades WHERE alpha_id = ? ORDER BY closed_at").all(alphaId) as { pnl: number }[];
  const pnlValues = pnlList.map((r) => r.pnl);
  const mean = pnlValues.length > 0 ? pnlValues.reduce((a, b) => a + b, 0) / pnlValues.length : 0;
  const stdDev = pnlValues.length > 1 ? Math.sqrt(pnlValues.reduce((s, v) => s + (v - mean) ** 2, 0) / (pnlValues.length - 1)) : 1;
  const sharpe = stdDev !== 0 ? (mean / stdDev) * Math.sqrt(252) : 0;

  let equity = 0;
  let runningMax = 0;
  let maxDrawdown = 0;
  let currentWins = 0;
  let currentLosses = 0;
  let consecutiveWins = 0;
  let consecutiveLosses = 0;

  for (const pnl of pnlValues) {
    equity += pnl;
    runningMax = Math.max(runningMax, equity);
    maxDrawdown = Math.min(maxDrawdown, equity - runningMax);

    if (pnl > 0) {
      currentWins += 1;
      currentLosses = 0;
    } else {
      currentLosses += 1;
      currentWins = 0;
    }

    consecutiveWins = Math.max(consecutiveWins, currentWins);
    consecutiveLosses = Math.max(consecutiveLosses, currentLosses);
  }

  const result: AlphaStats = {
    alpha_id: alphaId,
    total_trades: row.total_trades || 0,
    win_trades: row.win_trades || 0,
    loss_trades: row.loss_trades || 0,
    winrate: row.winrate || 0,
    total_pnl: row.total_pnl || 0,
    avg_pnl: row.avg_pnl || 0,
    avg_win: row.avg_win || 0,
    avg_loss: row.avg_loss || 0,
    max_drawdown: Math.round(maxDrawdown * 10000) / 10000,
    sharpe_ratio: Math.round(sharpe * 100) / 100,
    consecutive_wins: consecutiveWins,
    consecutive_losses: consecutiveLosses,
  };

  statsCache.set(alphaId, { expiresAt: now + STATS_CACHE_MS, value: result });
  return result;
}

export function getAlphaColumns(alphaId: string): ColumnSpec[] {
  const db = tryGetDb();
  if (!db) return [];
  return db.prepare("SELECT column_key as key, label, type, decimals, sort_order FROM alpha_columns WHERE alpha_id = ? ORDER BY sort_order").all(alphaId) as ColumnSpec[];
}

function buildDashboardData() {
  const db = tryGetDb();
  if (!db) return { total_pnl: 0, alphas: [], top_winners: [], top_losers: [] };
  const configs = parseAlphaConfigs();
  const alphaSummaries = db.prepare(`
      SELECT
        a.alpha_id,
        a.display_name,
        a.created_at,
        a.status,
        COALESCE(t.total_pnl, 0) AS pnl,
        COALESCE(t.winrate, 0) AS winrate,
        COALESCE(p.open_positions, 0) AS open_positions,
        COALESCE(d.today_trades, 0) AS today_trades
      FROM alphas a
      LEFT JOIN (
        SELECT
          alpha_id,
          ROUND(SUM(pnl), 4) AS total_pnl,
          ROUND(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS winrate
        FROM trades
        GROUP BY alpha_id
      ) t ON t.alpha_id = a.alpha_id
      LEFT JOIN (
        SELECT alpha_id, COUNT(*) AS open_positions
        FROM positions
        GROUP BY alpha_id
      ) p ON p.alpha_id = a.alpha_id
      LEFT JOIN (
        SELECT alpha_id, COUNT(*) AS today_trades
        FROM trades
        WHERE date(closed_at) = date('now')
        GROUP BY alpha_id
      ) d ON d.alpha_id = a.alpha_id
      ORDER BY a.created_at
  `).all().map((a: any) => ({
    ...a,
    pnl: Number(a.pnl || 0),
    winrate: Number(a.winrate || 0),
    open_positions: Number(a.open_positions || 0),
    today_trades: Number(a.today_trades || 0),
    config: configs[a.alpha_id] || {},
  }));

  const totalPnl = alphaSummaries.reduce((s, a) => s + a.pnl, 0);
  const topWinners = db.prepare("SELECT * FROM trades WHERE pnl > 0 ORDER BY pnl DESC LIMIT 5").all() as Trade[];
  const topLosers = db.prepare("SELECT * FROM trades WHERE pnl < 0 ORDER BY pnl ASC LIMIT 5").all() as Trade[];

  return {
    total_pnl: totalPnl,
    alphas: alphaSummaries,
    top_winners: topWinners,
    top_losers: topLosers,
  };
}

export function getDashboardData() {
  const now = Date.now();
  if (dashboardCache && dashboardCache.expiresAt > now) {
    return dashboardCache.value;
  }
  const value = buildDashboardData();
  dashboardCache = { expiresAt: now + DASHBOARD_CACHE_MS, value };
  return value;
}
