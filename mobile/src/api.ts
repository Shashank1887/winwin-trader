import * as SecureStore from "expo-secure-store";
import { Platform } from "react-native";

import {
  AppStateResponse,
  FnoContract,
  FnoStateResponse,
  IndexTrend,
  ServerConfig,
  TradingMode,
} from "./types";

const STORE_KEY = "winwin_mobile_server_config_v1";

const DEFAULT_CONFIG: ServerConfig = {
  baseUrl: "",
  apiToken: "",
  capital: "50000",
  maxSl: "400",
  maxTrades: "2",
  windowPreset: "morning",
  universePreset: "top15",
  customSymbols: "RELIANCE, TCS, TRENT, BEL, HAL",
  trailingSlEnabled: true,
  niftyTrendFilter: true,
};

export async function loadServerConfig(): Promise<ServerConfig> {
  try {
    let raw: string | null = null;
    if (Platform.OS === "web") {
      raw = typeof window !== "undefined" ? window.localStorage.getItem(STORE_KEY) : null;
    } else {
      raw = await SecureStore.getItemAsync(STORE_KEY);
    }
    if (!raw) return DEFAULT_CONFIG;
    return { ...DEFAULT_CONFIG, ...JSON.parse(raw) };
  } catch {
    return DEFAULT_CONFIG;
  }
}

export async function saveServerConfig(cfg: ServerConfig): Promise<void> {
  const normalized: ServerConfig = {
    ...cfg,
    baseUrl: cfg.baseUrl.trim().replace(/\/+$/, ""),
    apiToken: cfg.apiToken.trim(),
  };
  const serialized = JSON.stringify(normalized);
  if (Platform.OS === "web") {
    if (typeof window !== "undefined") {
      window.localStorage.setItem(STORE_KEY, serialized);
    }
    return;
  }
  await SecureStore.setItemAsync(STORE_KEY, serialized);
}

async function apiRequest<T>(
  baseUrl: string,
  apiToken: string,
  path: string,
  options: RequestInit = {},
  timeoutMs = 35000
): Promise<T> {
  const cleanBase = baseUrl.trim().replace(/\/+$/, "");
  if (!cleanBase) {
    throw new Error("Set your HTTPS Backend Server URL in Settings first.");
  }
  if (!apiToken.trim()) {
    throw new Error("Set your Mobile API Token (MOBILE_API_TOKEN) in Settings first.");
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(`${cleanBase}${path}`, {
      ...options,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${apiToken.trim()}`,
        ...(options.headers || {}),
      },
    });

    const text = await response.text();
    let parsed: any = null;
    try {
      parsed = text ? JSON.parse(text) : {};
    } catch {
      parsed = { detail: text };
    }

    if (!response.ok) {
      const msg = parsed?.detail || parsed?.message || `HTTP ${response.status}`;
      throw new Error(String(msg));
    }
    return parsed as T;
  } catch (err: any) {
    if (err?.name === "AbortError") {
      throw new Error("Request timed out. Check your 4G/5G connection or backend server.");
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

export function fetchState(
  cfg: ServerConfig,
  mode: TradingMode
): Promise<AppStateResponse> {
  const qs = new URLSearchParams({
    mode,
    capital: cfg.capital || "50000",
    max_sl: cfg.maxSl || "400",
    max_trades: cfg.maxTrades || "2",
    window_preset: cfg.windowPreset || "morning",
    trailing_sl_enabled: String(cfg.trailingSlEnabled !== false),
    nifty_trend_filter: String(cfg.niftyTrendFilter !== false),
  }).toString();
  return apiRequest<AppStateResponse>(cfg.baseUrl, cfg.apiToken, `/api/state?${qs}`);
}

export function scanMarket(
  cfg: ServerConfig,
  mode: TradingMode,
  previewOutsideWindow: boolean,
  autoExecute: boolean
): Promise<AppStateResponse> {
  return apiRequest<AppStateResponse>(
    cfg.baseUrl,
    cfg.apiToken,
    "/api/scan",
    {
      method: "POST",
      body: JSON.stringify({
        mode,
        capital: Number(cfg.capital || 50000),
        max_sl: Number(cfg.maxSl || 400),
        max_trades: Number(cfg.maxTrades || 2),
        window_preset: cfg.windowPreset || "morning",
        universe_preset: cfg.universePreset || "top15",
        custom_symbols: cfg.customSymbols || "",
        trailing_sl_enabled: cfg.trailingSlEnabled !== false,
        nifty_trend_filter: cfg.niftyTrendFilter !== false,
        preview_outside_window: previewOutsideWindow,
        auto_execute: autoExecute,
      }),
    },
    120000
  );
}

export function executeSignalTrade(
  cfg: ServerConfig,
  mode: TradingMode,
  symbol: string
): Promise<AppStateResponse> {
  return apiRequest<AppStateResponse>(cfg.baseUrl, cfg.apiToken, "/api/execute", {
    method: "POST",
    body: JSON.stringify({
      mode,
      symbol,
      capital: Number(cfg.capital || 50000),
      max_sl: Number(cfg.maxSl || 400),
      max_trades: Number(cfg.maxTrades || 2),
      window_preset: cfg.windowPreset || "morning",
    }),
  });
}

export function refreshLtps(
  cfg: ServerConfig,
  mode: TradingMode
): Promise<AppStateResponse> {
  return apiRequest<AppStateResponse>(cfg.baseUrl, cfg.apiToken, "/api/refresh-ltps", {
    method: "POST",
    body: JSON.stringify({
      mode,
      capital: Number(cfg.capital || 50000),
      max_sl: Number(cfg.maxSl || 400),
      max_trades: Number(cfg.maxTrades || 2),
      window_preset: cfg.windowPreset || "morning",
    }),
  });
}

export function panicSquareOffAll(
  cfg: ServerConfig,
  mode: TradingMode
): Promise<AppStateResponse> {
  return apiRequest<AppStateResponse>(cfg.baseUrl, cfg.apiToken, "/api/panic-exit", {
    method: "POST",
    body: JSON.stringify({
      mode,
      capital: Number(cfg.capital || 50000),
      max_sl: Number(cfg.maxSl || 400),
      max_trades: Number(cfg.maxTrades || 2),
      window_preset: cfg.windowPreset || "morning",
    }),
  });
}

export function configureAutomation(
  cfg: ServerConfig,
  params: {
    enabled: boolean;
    mode: TradingMode;
    scanIntervalSec: number;
    ltpIntervalSec: number;
    autoExecute: boolean;
    autoSquareOffEod: boolean;
  }
): Promise<AppStateResponse> {
  return apiRequest<AppStateResponse>(cfg.baseUrl, cfg.apiToken, "/api/automation", {
    method: "POST",
    body: JSON.stringify({
      enabled: params.enabled,
      mode: params.mode,
      scan_interval_sec: params.scanIntervalSec,
      ltp_interval_sec: params.ltpIntervalSec,
      auto_execute: params.autoExecute,
      auto_square_off_eod: params.autoSquareOffEod,
      capital: Number(cfg.capital || 50000),
      max_sl: Number(cfg.maxSl || 400),
      max_trades: Number(cfg.maxTrades || 2),
      window_preset: cfg.windowPreset || "morning",
      universe_preset: cfg.universePreset || "top15",
      custom_symbols: cfg.customSymbols || "",
      trailing_sl_enabled: cfg.trailingSlEnabled !== false,
      nifty_trend_filter: cfg.niftyTrendFilter !== false,
    }),
  });
}

export function fetchFnoState(
  cfg: ServerConfig,
  mode: TradingMode
): Promise<FnoStateResponse> {
  const qs = new URLSearchParams({
    mode,
    window_preset: cfg.windowPreset || "morning",
  }).toString();
  return apiRequest<FnoStateResponse>(cfg.baseUrl, cfg.apiToken, `/api/fno/state?${qs}`);
}

export function analyzeFno(
  cfg: ServerConfig,
  mode: TradingMode,
  underlying: "NIFTY" | "BANKNIFTY" | "FINNIFTY",
  instrumentType: "OPTIDX" | "FUTIDX"
): Promise<{ trend: IndexTrend; contracts: FnoContract[] }> {
  return apiRequest(cfg.baseUrl, cfg.apiToken, "/api/fno/analyze", {
    method: "POST",
    body: JSON.stringify({ mode, underlying, instrument_type: instrumentType }),
  });
}

export function executeFnoTrade(
  cfg: ServerConfig,
  payload: {
    mode: TradingMode;
    tradingsymbol: string;
    token: string;
    qty: number;
    entryPrice: number;
    slPoints: number;
    segment: "NFO_OPTIONS" | "NFO_FUTURES";
    riskAck: boolean;
  }
): Promise<FnoStateResponse> {
  return apiRequest<FnoStateResponse>(cfg.baseUrl, cfg.apiToken, "/api/fno/execute", {
    method: "POST",
    body: JSON.stringify({
      mode: payload.mode,
      tradingsymbol: payload.tradingsymbol,
      token: payload.token,
      qty: payload.qty,
      entry_price: payload.entryPrice,
      sl_points: payload.slPoints,
      segment: payload.segment,
      window_preset: cfg.windowPreset || "morning",
      risk_ack: payload.riskAck,
    }),
  });
}

export function panicSquareOffFno(
  cfg: ServerConfig,
  mode: TradingMode
): Promise<FnoStateResponse> {
  return apiRequest<FnoStateResponse>(cfg.baseUrl, cfg.apiToken, "/api/fno/panic-exit", {
    method: "POST",
    body: JSON.stringify({
      mode,
      window_preset: cfg.windowPreset || "morning",
    }),
  });
}

export function connectAngelBroker(
  cfg: ServerConfig,
  creds: { apiKey: string; clientId: string; password: string; totpSecret: string }
): Promise<{ connected: boolean; profile_name: string; message: string }> {
  return apiRequest(cfg.baseUrl, cfg.apiToken, "/api/broker/connect", {
    method: "POST",
    body: JSON.stringify({
      api_key: creds.apiKey,
      client_id: creds.clientId,
      password: creds.password,
      totp_secret: creds.totpSecret,
    }),
  });
}

export function disconnectAngelBroker(
  cfg: ServerConfig
): Promise<{ connected: boolean; message: string }> {
  return apiRequest(cfg.baseUrl, cfg.apiToken, "/api/broker/disconnect", {
    method: "POST",
  });
}
