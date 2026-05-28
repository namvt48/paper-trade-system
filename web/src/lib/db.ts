import Database from "better-sqlite3";
import fs from "fs";
import path from "path";
import type { Alpha, Position, Trade, EquityPoint, AlphaStats, AlphaConfig } from "./types";
import { createLogger } from "./logger";

const log = createLogger("db");

const DB_PATH = process.env.DB_PATH || path.join(process.cwd(), "data", "paper-trade.db");
const ALPHAS_DIR = process.env.ALPHAS_DIR || path.join(process.cwd(), "alphas");

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
  const configs: Record<string, AlphaConfig> = {};
  try {
    const entries = fs.readdirSync(ALPHAS_DIR, { withFileTypes: true });
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const envPath = path.join(ALPHAS_DIR, entry.name, ".env");
      if (!fs.existsSync(envPath)) continue;
      const raw = fs.readFileSync(envPath, "utf-8");
      const parsed = parseDotEnv(raw);
      const alphaId = parsed["ALPHA_ID"] || entry.name;
      configs[alphaId] = parsed as AlphaConfig;
    }
  } catch {
    return {};
  }
  return configs;
}

export function getAlphaConfig(alphaId: string): AlphaConfig {
  return parseAlphaConfigs()[alphaId] || {};
}

function getDb(): Database.Database {
  return new Database(DB_PATH, { readonly: true, fileMustExist: true });
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
  try {
    return db.prepare("SELECT * FROM alphas ORDER BY created_at").all() as Alpha[];
  } finally {
    db.close();
  }
}

export function getAlpha(alphaId: string): Alpha | undefined {
  const db = tryGetDb();
  if (!db) return undefined;
  try {
    return db.prepare("SELECT * FROM alphas WHERE alpha_id = ?").get(alphaId) as Alpha | undefined;
  } finally {
    db.close();
  }
}

export function getOpenPositions(alphaId?: string): Position[] {
  const db = tryGetDb();
  if (!db) return [];
  try {
    if (alphaId) {
      return db.prepare("SELECT * FROM positions WHERE alpha_id = ? ORDER BY opened_at DESC").all(alphaId) as Position[];
    }
    return db.prepare("SELECT * FROM positions ORDER BY opened_at DESC").all() as Position[];
  } finally {
    db.close();
  }
}

export function getTrades(alphaId: string, limit = 100, offset = 0): Trade[] {
  const db = tryGetDb();
  if (!db) return [];
  try {
    return db.prepare("SELECT * FROM trades WHERE alpha_id = ? ORDER BY closed_at DESC LIMIT ? OFFSET ?").all(alphaId, limit, offset) as Trade[];
  } finally {
    db.close();
  }
}

export function getEquityCurve(alphaId: string): EquityPoint[] {
  const db = tryGetDb();
  if (!db) return [];
  try {
    return db.prepare(`
      SELECT closed_at, SUM(pnl) OVER (ORDER BY closed_at) as equity
      FROM trades WHERE alpha_id = ?
      ORDER BY closed_at
    `).all(alphaId) as EquityPoint[];
  } finally {
    db.close();
  }
}

export function getCompareEquity(alphaIds: string[]): Map<string, EquityPoint[]> {
  const result = new Map<string, EquityPoint[]>();
  for (const id of alphaIds) {
    result.set(id, getEquityCurve(id));
  }
  return result;
}

export function getAlphaStats(alphaId: string): AlphaStats {
  const db = tryGetDb();
  if (!db) return { alpha_id: alphaId, total_trades: 0, win_trades: 0, loss_trades: 0, winrate: 0, total_pnl: 0, avg_pnl: 0, avg_win: 0, avg_loss: 0, max_drawdown: 0, sharpe_ratio: 0, consecutive_wins: 0, consecutive_losses: 0 };
  try {
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

    return {
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
  } finally {
    db.close();
  }
}

export function getDashboardData() {
  const db = tryGetDb();
  if (!db) return { total_pnl: 0, alphas: [], top_winners: [], top_losers: [] };
  try {
    const alphas = getAllAlphas();
    const alphaSummaries = alphas.map((a) => {
      const stats = getAlphaStats(a.alpha_id);
      const openPositions = db.prepare("SELECT COUNT(*) as count FROM positions WHERE alpha_id = ?").get(a.alpha_id) as any;
      const todayTrades = db.prepare("SELECT COUNT(*) as count FROM trades WHERE alpha_id = ? AND date(closed_at) = date('now')").get(a.alpha_id) as any;
      return {
        ...a,
        pnl: stats.total_pnl,
        winrate: stats.winrate,
        open_positions: openPositions?.count || 0,
        today_trades: todayTrades?.count || 0,
        config: getAlphaConfig(a.alpha_id),
      };
    });

    const totalPnl = alphaSummaries.reduce((s, a) => s + a.pnl, 0);
    const topWinners = db.prepare("SELECT * FROM trades WHERE pnl > 0 ORDER BY pnl DESC LIMIT 5").all() as Trade[];
    const topLosers = db.prepare("SELECT * FROM trades WHERE pnl < 0 ORDER BY pnl ASC LIMIT 5").all() as Trade[];

    return {
      total_pnl: totalPnl,
      alphas: alphaSummaries,
      top_winners: topWinners,
      top_losers: topLosers,
    };
  } finally {
    db.close();
  }
}
