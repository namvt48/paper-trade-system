type Level = "DEBUG" | "INFO" | "WARN" | "ERROR";

const LEVEL_RANK: Record<Level, number> = { DEBUG: 0, INFO: 1, WARN: 2, ERROR: 3 };

const MIN_LEVEL: Level = (() => {
  const env = process.env.LOG_LEVEL?.toUpperCase();
  if (env && env in LEVEL_RANK) return env as Level;
  return process.env.NODE_ENV === "production" ? "INFO" : "DEBUG";
})();

function emit(level: Level, mod: string, msg: string, extra?: Record<string, unknown>) {
  if (LEVEL_RANK[level] < LEVEL_RANK[MIN_LEVEL]) return;
  const entry: Record<string, unknown> = {
    ts: new Date().toISOString(),
    lvl: level,
    mod,
    msg,
    ...extra,
  };
  const line = JSON.stringify(entry) + "\n";
  if (level === "ERROR" || level === "WARN") {
    process.stderr.write(line);
  } else {
    process.stdout.write(line);
  }
}

export interface Logger {
  debug(msg: string, extra?: Record<string, unknown>): void;
  info(msg: string, extra?: Record<string, unknown>): void;
  warn(msg: string, extra?: Record<string, unknown>): void;
  error(msg: string, extra?: Record<string, unknown>): void;
}

export function createLogger(mod: string): Logger {
  return {
    debug: (msg, extra) => emit("DEBUG", mod, msg, extra),
    info:  (msg, extra) => emit("INFO",  mod, msg, extra),
    warn:  (msg, extra) => emit("WARN",  mod, msg, extra),
    error: (msg, extra) => emit("ERROR", mod, msg, extra),
  };
}
