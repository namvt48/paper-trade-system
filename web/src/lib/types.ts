export interface Alpha {
  alpha_id: string;
  display_name: string;
  created_at: string;
  status: string;
}

export interface Position {
  position_id: string;
  alpha_id: string;
  signal_id: string;
  symbol: string;
  side: "LONG" | "SHORT";
  entry_price: number;
  qty: number;
  tp: number | null;
  sl: number | null;
  leverage: number;
  opened_at: string;
  metadata: string;
}

export interface Trade {
  trade_id: string;
  position_id: string;
  alpha_id: string;
  signal_id: string;
  symbol: string;
  side: "LONG" | "SHORT";
  entry_price: number;
  exit_price: number;
  qty: number;
  pnl: number;
  pnl_percent: number;
  leverage: number;
  tp: number | null;
  sl: number | null;
  reason: string;
  duration_hours: number;
  opened_at: string;
  closed_at: string;
  metadata: string;
}

export interface EquityPoint {
  closed_at: string;
  equity: number;
}

export interface AlphaStats {
  alpha_id: string;
  total_trades: number;
  win_trades: number;
  loss_trades: number;
  winrate: number;
  total_pnl: number;
  avg_pnl: number;
  avg_win: number;
  avg_loss: number;
  max_drawdown: number;
  sharpe_ratio: number;
  consecutive_wins: number;
  consecutive_losses: number;
}

export type AlphaConfigValue = string | number | boolean | null;

export type AlphaConfig = Record<string, AlphaConfigValue>;

export interface DashboardData {
  total_pnl: number;
  alphas: (Alpha & { pnl: number; winrate: number; open_positions: number; today_trades: number; config: AlphaConfig })[];
  top_winners: Trade[];
  top_losers: Trade[];
}
