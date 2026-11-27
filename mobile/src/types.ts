export type TradingMode = "PAPER" | "LIVE";

export type SystemStatus = "ACTIVE" | "GOAL LOCKED" | "KILL-SWITCH LOCKED";

export type WindowPreset = "morning" | "split" | "full";
export type UniversePreset = "top15" | "nifty50" | "custom";

export interface IndexTrend {
  index_name: string;
  candle_time: string;
  price: number;
  ema_9: number;
  ema_21: number;
  rsi_14: number;
  regime: "BULLISH" | "BEARISH" | "NEUTRAL";
  recommended_fno_action: "BUY_CE" | "BUY_PE" | "WAIT";
  error?: string | null;
}

export interface FnoContract {
  name: string;
  instrumenttype: string;
  tradingsymbol: string;
  token: string;
  expiry: string;
  strike: number;
  lotsize: number;
  ltp?: number;
}

export interface FnoStateResponse {
  mode: TradingMode;
  status: string;
  can_trade: boolean;
  within_active_window: boolean;
  daily_target: number;
  daily_max_loss: number;
  summary: {
    realized_pnl: number;
    gross_realized_pnl: number;
    total_charges: number;
    unrealized_pnl: number;
    net_pnl: number;
    trades_used: number;
    open_positions_count: number;
  };
  trades: Array<{
    trade_id: string;
    timestamp: string;
    segment: string;
    tradingsymbol: string;
    qty: number;
    entry_price: number;
    stop_loss: number;
    target: number;
    ltp_or_exit: number;
    charges: number;
    pnl: number;
    status: "OPEN" | "CLOSED";
    exit_reason: string;
  }>;
  notice?: string | null;
}

export interface RiskSnapshot {
  realized_pnl: number;
  gross_realized_pnl?: number;
  total_charges?: number;
  unrealized_pnl: number;
  net_pnl: number;
  trades_used: number;
  max_trades: number;
  open_positions_count: number;
  system_status: SystemStatus;
  status_reason: string;
  within_active_window: boolean;
  can_trade: boolean;
  goal_progress_pct: number;
}

export interface SignalItem {
  symbol: string;
  tradingsymbol: string;
  token: string;
  candle_time: string;
  price: number;
  vwap: number;
  ema_9: number;
  ema_21: number;
  rsi_14: number;
  volume_ratio: number;
  stop_loss: number;
  target: number;
  qty: number;
  sl_distance: number;
  expected_profit: number;
  is_buy_signal: boolean;
  cond_price_above_vwap: boolean;
  cond_ema_bullish_crossover: boolean;
  cond_rsi_and_volume: boolean;
  error?: string | null;
}

export interface TradeItem {
  trade_id: string;
  timestamp: string;
  mode: TradingMode;
  symbol: string;
  qty: number;
  entry_price: number;
  stop_loss: number;
  target: number;
  ltp_or_exit: number;
  pnl: number;
  status: "OPEN" | "CLOSED";
  exit_reason: string;
  entry_order_id: string;
}

export interface AutomationStatus {
  enabled: boolean;
  mode: TradingMode;
  scan_interval_sec: number;
  ltp_interval_sec: number;
  auto_execute: boolean;
  auto_square_off_eod: boolean;
  state: string;
  activity_log: string[];
}

export interface AppStateResponse {
  time_ist: string;
  mode: TradingMode;
  broker_connected: boolean;
  broker_profile: string;
  last_scan_time: string;
  data_source: string;
  index_trend?: IndexTrend | null;
  guardrails: {
    daily_profit_target: number;
    daily_max_loss: number;
    max_sl_per_trade: number;
    max_trades_per_day: number;
    active_window: string;
  };
  risk_snapshot: RiskSnapshot;
  automation?: AutomationStatus;
  signals: SignalItem[];
  diagnostics: SignalItem[];
  trades: TradeItem[];
  notice?: string | null;
}

export interface ServerConfig {
  baseUrl: string;
  apiToken: string;
  capital: string;
  maxSl: string;
  maxTrades: string;
  windowPreset: WindowPreset;
  universePreset: UniversePreset;
  customSymbols: string;
  trailingSlEnabled: boolean;
  niftyTrendFilter: boolean;
}
