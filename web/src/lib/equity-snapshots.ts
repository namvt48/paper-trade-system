import Database from "better-sqlite3";
import fs from "fs";
import path from "path";
import type { EquitySnapshot } from "./types";
import { createLogger } from "./logger";

const log = createLogger("equity-snapshots");

const SNAPSHOT_DB_PATH = process.env.EQUITY_SNAPSHOT_DB_PATH || path.join(process.cwd(), "data", "equity-snapshots.db");
const SNAPSHOT_MAX_POINTS = Number(process.env.EQUITY_SNAPSHOT_MAX_POINTS || "1000");

declare global {
  // eslint-disable-next-line no-var
  var __equitySnapshotDb: Database.Database | undefined;
}

function getSnapshotDb(): Database.Database {
  if (!globalThis.__equitySnapshotDb) {
    globalThis.__equitySnapshotDb = new Database(SNAPSHOT_DB_PATH, { readonly: true, fileMustExist: true });
    globalThis.__equitySnapshotDb.pragma("query_only = ON");
    globalThis.__equitySnapshotDb.pragma("busy_timeout = 5000");
  }
  return globalThis.__equitySnapshotDb;
}

function tryGetSnapshotDb(): Database.Database | null {
  try {
    return getSnapshotDb();
  } catch (err) {
    log.error("failed to open snapshot database", { path: SNAPSHOT_DB_PATH, err: String(err) });
    return null;
  }
}

export function isSnapshotDbAvailable(): boolean {
  return fs.existsSync(SNAPSHOT_DB_PATH);
}

const TOTAL_KEY = "__TOTAL__";

const INTERVAL_SECONDS: Record<string, number> = {
  "5m": 300,
  "15m": 900,
  "30m": 1800,
  "1h": 3600,
};

function selectInterval(rawCount: number): string {
  if (rawCount <= 500) return "5m";
  if (rawCount <= 2000) return "15m";
  if (rawCount <= 5000) return "30m";
  return "1h";
}

function downsampleSnapshots(rows: EquitySnapshot[], maxPoints: number): EquitySnapshot[] {
  if (maxPoints <= 0 || rows.length <= maxPoints) return rows;
  const sampled: EquitySnapshot[] = [];
  const step = (rows.length - 1) / (maxPoints - 1);
  for (let i = 0; i < maxPoints; i += 1) {
    sampled.push(rows[Math.round(i * step)]);
  }
  return sampled;
}

export function getEquitySnapshots(alphaId: string, maxPoints = SNAPSHOT_MAX_POINTS): EquitySnapshot[] {
  const db = tryGetSnapshotDb();
  if (!db) return [];

  const tableExists = db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='equity_snapshots'").get();
  if (!tableExists) return [];

  const targetAlpha = alphaId || TOTAL_KEY;

  const countRow = db.prepare("SELECT COUNT(*) as cnt FROM equity_snapshots WHERE alpha_id = ?").get(targetAlpha) as { cnt: number };
  const rawCount = countRow.cnt;
  if (rawCount === 0) return [];

  const interval = selectInterval(rawCount);
  const intervalSec = INTERVAL_SECONDS[interval];

  let rows: EquitySnapshot[];
  if (interval === "5m") {
    rows = db.prepare(`
      SELECT timestamp, balance
      FROM equity_snapshots WHERE alpha_id = ? ORDER BY timestamp ASC
    `).all(targetAlpha) as EquitySnapshot[];
  } else {
    rows = db.prepare(`
      WITH buckets AS (
        SELECT *,
          (strftime('%s', timestamp) / ?) AS bucket_id
        FROM equity_snapshots WHERE alpha_id = ?
      )
      SELECT timestamp, balance
      FROM (
        SELECT *,
          ROW_NUMBER() OVER (PARTITION BY bucket_id ORDER BY timestamp DESC) as rn
        FROM buckets
      ) WHERE rn = 1
      ORDER BY timestamp ASC
    `).all(intervalSec, targetAlpha) as EquitySnapshot[];
  }

  return downsampleSnapshots(rows, maxPoints);
}

export function getRawEquitySnapshots(alphaId: string): EquitySnapshot[] {
  const db = tryGetSnapshotDb();
  if (!db) return [];

  const tableExists = db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='equity_snapshots'").get();
  if (!tableExists) return [];

  const targetAlpha = alphaId || TOTAL_KEY;
  return db.prepare(`
    SELECT timestamp, balance
    FROM equity_snapshots WHERE alpha_id = ? ORDER BY timestamp ASC
  `).all(targetAlpha) as EquitySnapshot[];
}
