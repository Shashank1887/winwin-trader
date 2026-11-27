"""Intraday Nifty 50 screener indicators and BUY setup evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from risk_manager import IST, MAX_SL_PER_TRADE, calculate_position_sizing

UniversePreset = Literal["top15", "nifty50", "custom"]

# Top 15 high-liquidity Nifty 50 stocks with verified Angel One NSE equity tokens
NIFTY50_LIQUID_UNIVERSE: list[dict[str, str]] = [
    {"symbol": "RELIANCE", "tradingsymbol": "RELIANCE-EQ", "token": "2885", "yahoo_symbol": "RELIANCE.NS"},
    {"symbol": "HDFCBANK", "tradingsymbol": "HDFCBANK-EQ", "token": "1333", "yahoo_symbol": "HDFCBANK.NS"},
    {"symbol": "ICICIBANK", "tradingsymbol": "ICICIBANK-EQ", "token": "4963", "yahoo_symbol": "ICICIBANK.NS"},
    {"symbol": "INFY", "tradingsymbol": "INFY-EQ", "token": "1594", "yahoo_symbol": "INFY.NS"},
    {"symbol": "TCS", "tradingsymbol": "TCS-EQ", "token": "11536", "yahoo_symbol": "TCS.NS"},
    {"symbol": "SBIN", "tradingsymbol": "SBIN-EQ", "token": "3045", "yahoo_symbol": "SBIN.NS"},
    {"symbol": "TATAMOTORS", "tradingsymbol": "TMPV-EQ", "token": "3456", "yahoo_symbol": "TMPV.NS"},
    {"symbol": "AXISBANK", "tradingsymbol": "AXISBANK-EQ", "token": "5900", "yahoo_symbol": "AXISBANK.NS"},
    {"symbol": "LT", "tradingsymbol": "LT-EQ", "token": "11483", "yahoo_symbol": "LT.NS"},
    {"symbol": "BHARTIARTL", "tradingsymbol": "BHARTIARTL-EQ", "token": "10604", "yahoo_symbol": "BHARTIARTL.NS"},
    {"symbol": "KOTAKBANK", "tradingsymbol": "KOTAKBANK-EQ", "token": "1922", "yahoo_symbol": "KOTAKBANK.NS"},
    {"symbol": "ITC", "tradingsymbol": "ITC-EQ", "token": "1660", "yahoo_symbol": "ITC.NS"},
    {"symbol": "BAJFINANCE", "tradingsymbol": "BAJFINANCE-EQ", "token": "317", "yahoo_symbol": "BAJFINANCE.NS"},
    {"symbol": "SUNPHARMA", "tradingsymbol": "SUNPHARMA-EQ", "token": "3351", "yahoo_symbol": "SUNPHARMA.NS"},
    {"symbol": "MARUTI", "tradingsymbol": "MARUTI-EQ", "token": "10999", "yahoo_symbol": "MARUTI.NS"},
]

# Full 50 Nifty 50 constituents (display symbol, NSE equity symbol, Yahoo symbol)
NIFTY50_ALL_SYMBOLS: list[str] = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "BPCL", "BRITANNIA", "CIPLA", "COALINDIA", "DRREDDY",
    "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK",
    "INFY", "ITC", "JSWSTEEL", "KOTAKBANK", "LT",
    "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
    "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATASTEEL", "TCS",
    "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
]


def parse_custom_symbols(raw: str) -> list[str]:
    """Parse comma- or newline-separated NSE stock symbols cleanly."""
    seen: set[str] = set()
    parsed: list[str] = []
    for chunk in raw.replace("\n", ",").replace(";", ",").split(","):
        sym = chunk.strip().upper().removesuffix(".NS").removesuffix("-EQ")
        if sym and sym not in seen:
            seen.add(sym)
            parsed.append(sym)
    return parsed


@dataclass(frozen=True)
class StockEvaluation:
    """Indicator values, rule flags, and sizing output for one scanned stock."""

    symbol: str
    tradingsymbol: str
    token: str
    candle_time: str
    price: float
    vwap: float
    ema_9: float
    ema_21: float
    prev_ema_9: float
    prev_ema_21: float
    rsi_14: float
    volume: float
    avg_volume_20: float
    volume_ratio: float
    cond_price_above_vwap: bool
    cond_ema_bullish_crossover: bool
    cond_rsi_and_volume: bool
    is_buy_signal: bool
    stop_loss: float
    target: float
    qty: int
    sl_distance: float
    expected_profit: float
    error: str | None = None


def compute_intraday_vwap(df: pd.DataFrame) -> pd.Series:
    """Compute session-reset intraday VWAP using IST trading dates."""
    if df.empty:
        return pd.Series(dtype="float64")

    timestamps = pd.to_datetime(df["timestamp"])
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize(IST)
    else:
        timestamps = timestamps.dt.tz_convert(IST)

    session_date = timestamps.dt.date
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    tp_vol = typical_price * df["volume"]

    cum_tp_vol = tp_vol.groupby(session_date).cumsum()
    cum_vol = df["volume"].groupby(session_date).cumsum().replace(0, np.nan)
    return cum_tp_vol / cum_vol


def compute_ema(series: pd.Series, span: int) -> pd.Series:
    """Compute exponential moving average."""
    return series.ewm(span=span, adjust=False).mean()


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Compute Wilder's RSI(14)."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # When avg_loss is 0 and avg_gain > 0, RSI is 100
    rsi = rsi.where(avg_loss != 0.0, np.where(avg_gain > 0.0, 100.0, 50.0))
    return rsi


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute Wilder's Average True Range."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def evaluate_stock_setup(
    symbol: str,
    tradingsymbol: str,
    token: str,
    candles: pd.DataFrame,
    capital: float = 50000.0,
    max_sl_rupees: float = MAX_SL_PER_TRADE,
    live_ltp: float | None = None,
) -> StockEvaluation:
    """Evaluate all 3 intraday BUY conditions on 5-minute OHLCV candles."""
    required_cols = {"timestamp", "open", "high", "low", "close", "volume"}
    if candles is None or candles.empty or not required_cols.issubset(candles.columns):
        return _error_eval(symbol, tradingsymbol, token, "Insufficient or malformed candle data.")

    df = candles.sort_values("timestamp").reset_index(drop=True).copy()
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)

    if len(df) < 22:
        return _error_eval(
            symbol, tradingsymbol, token, f"Need at least 22 5-min candles, got {len(df)}."
        )

    df["vwap"] = compute_intraday_vwap(df)
    df["ema_9"] = compute_ema(df["close"], 9)
    df["ema_21"] = compute_ema(df["close"], 21)
    df["rsi_14"] = compute_rsi(df["close"], 14)
    df["atr_14"] = compute_atr(df, 14)

    curr = df.iloc[-1]
    prev = df.iloc[-2]

    # Use preceding 20 candles as the baseline average volume so a spike doesn't dilute itself
    prior_volumes = df["volume"].iloc[-21:-1]
    avg_vol_20 = float(prior_volumes.mean()) if len(prior_volumes) > 0 else float(df["volume"].mean())
    curr_vol = float(curr["volume"])
    vol_ratio = round(curr_vol / avg_vol_20, 2) if avg_vol_20 > 0 else 0.0

    price = float(live_ltp) if (live_ltp is not None and live_ltp > 0) else float(curr["close"])
    vwap = float(curr["vwap"]) if pd.notna(curr["vwap"]) else price
    ema_9 = float(curr["ema_9"])
    ema_21 = float(curr["ema_21"])
    prev_ema_9 = float(prev["ema_9"])
    prev_ema_21 = float(prev["ema_21"])
    rsi_14 = float(curr["rsi_14"]) if pd.notna(curr["rsi_14"]) else 50.0
    atr_14 = float(curr["atr_14"]) if pd.notna(curr["atr_14"]) else price * 0.004

    cond_price_above_vwap = price > vwap
    cond_ema_bullish_crossover = (ema_9 > ema_21) and (prev_ema_9 <= prev_ema_21)
    cond_rsi_and_volume = (55.0 <= rsi_14 <= 68.0) and (avg_vol_20 > 0) and (curr_vol > 1.5 * avg_vol_20)

    # Technical stop-loss anchored to recent 3-candle swing low & ATR buffer
    recent_low = float(df["low"].iloc[-3:].min())
    min_sl_dist = max(price * 0.0025, 0.50)
    atr_sl = price - max(atr_14, min_sl_dist)
    raw_sl = min(recent_low, atr_sl)
    if raw_sl >= price - min_sl_dist:
        raw_sl = price - min_sl_dist

    sizing = calculate_position_sizing(
        entry_price=price,
        stop_loss_price=raw_sl,
        capital=capital,
        max_sl_rupees=max_sl_rupees,
    )

    is_buy = bool(
        cond_price_above_vwap
        and cond_ema_bullish_crossover
        and cond_rsi_and_volume
        and sizing.qty >= 1
    )

    ts = pd.to_datetime(curr["timestamp"])
    if ts.tzinfo is None:
        ts = ts.tz_localize(IST)
    else:
        ts = ts.tz_convert(IST)

    return StockEvaluation(
        symbol=symbol,
        tradingsymbol=tradingsymbol,
        token=token,
        candle_time=ts.strftime("%Y-%m-%d %H:%M IST"),
        price=round(price, 2),
        vwap=round(vwap, 2),
        ema_9=round(ema_9, 2),
        ema_21=round(ema_21, 2),
        prev_ema_9=round(prev_ema_9, 2),
        prev_ema_21=round(prev_ema_21, 2),
        rsi_14=round(rsi_14, 2),
        volume=round(curr_vol, 0),
        avg_volume_20=round(avg_vol_20, 0),
        volume_ratio=vol_ratio,
        cond_price_above_vwap=cond_price_above_vwap,
        cond_ema_bullish_crossover=cond_ema_bullish_crossover,
        cond_rsi_and_volume=cond_rsi_and_volume,
        is_buy_signal=is_buy,
        stop_loss=sizing.stop_loss,
        target=sizing.target,
        qty=sizing.qty,
        sl_distance=sizing.sl_distance,
        expected_profit=sizing.target_profit_rupees,
        error=None,
    )


def _error_eval(symbol: str, tradingsymbol: str, token: str, error_msg: str) -> StockEvaluation:
    return StockEvaluation(
        symbol=symbol,
        tradingsymbol=tradingsymbol,
        token=token,
        candle_time="-",
        price=0.0,
        vwap=0.0,
        ema_9=0.0,
        ema_21=0.0,
        prev_ema_9=0.0,
        prev_ema_21=0.0,
        rsi_14=0.0,
        volume=0.0,
        avg_volume_20=0.0,
        volume_ratio=0.0,
        cond_price_above_vwap=False,
        cond_ema_bullish_crossover=False,
        cond_rsi_and_volume=False,
        is_buy_signal=False,
        stop_loss=0.0,
        target=0.0,
        qty=0,
        sl_distance=0.0,
        expected_profit=0.0,
        error=error_msg,
    )


@dataclass(frozen=True)
class IndexTrend:
    """5-minute trend regime for NIFTY 50 / BANKNIFTY index filter and F&O signals."""

    index_name: str
    candle_time: str
    price: float
    ema_9: float
    ema_21: float
    rsi_14: float
    regime: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    recommended_fno_action: Literal["BUY_CE", "BUY_PE", "WAIT"]
    error: str | None = None


def evaluate_index_trend(candles: pd.DataFrame, index_name: str = "NIFTY") -> IndexTrend:
    """Evaluate index spot 5m trend for Equity trend filtering and F&O CE/PE direction."""
    required_cols = {"timestamp", "open", "high", "low", "close"}
    if candles is None or candles.empty or not required_cols.issubset(candles.columns):
        return IndexTrend(
            index_name=index_name,
            candle_time="-",
            price=0.0,
            ema_9=0.0,
            ema_21=0.0,
            rsi_14=0.0,
            regime="NEUTRAL",
            recommended_fno_action="WAIT",
            error="Insufficient index candles.",
        )

    df = candles.sort_values("timestamp").reset_index(drop=True).copy()
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    if len(df) < 22:
        return IndexTrend(
            index_name=index_name,
            candle_time="-",
            price=0.0,
            ema_9=0.0,
            ema_21=0.0,
            rsi_14=0.0,
            regime="NEUTRAL",
            recommended_fno_action="WAIT",
            error=f"Need >= 22 index candles, got {len(df)}.",
        )

    df["ema_9"] = compute_ema(df["close"], 9)
    df["ema_21"] = compute_ema(df["close"], 21)
    df["rsi_14"] = compute_rsi(df["close"], 14)

    curr = df.iloc[-1]
    price = float(curr["close"])
    ema_9 = float(curr["ema_9"])
    ema_21 = float(curr["ema_21"])
    rsi_14 = float(curr["rsi_14"]) if pd.notna(curr["rsi_14"]) else 50.0

    ts = pd.to_datetime(curr["timestamp"])
    if ts.tzinfo is None:
        ts = ts.tz_localize(IST)
    else:
        ts = ts.tz_convert(IST)

    if price > ema_21 and ema_9 > ema_21:
        regime: Literal["BULLISH", "BEARISH", "NEUTRAL"] = "BULLISH"
        action: Literal["BUY_CE", "BUY_PE", "WAIT"] = "BUY_CE" if 52.0 <= rsi_14 <= 72.0 else "WAIT"
    elif price < ema_21 and ema_9 < ema_21:
        regime = "BEARISH"
        action = "BUY_PE" if 28.0 <= rsi_14 <= 48.0 else "WAIT"
    else:
        regime = "NEUTRAL"
        action = "WAIT"

    return IndexTrend(
        index_name=index_name,
        candle_time=ts.strftime("%Y-%m-%d %H:%M IST"),
        price=round(price, 2),
        ema_9=round(ema_9, 2),
        ema_21=round(ema_21, 2),
        rsi_14=round(rsi_14, 2),
        regime=regime,
        recommended_fno_action=action,
        error=None,
    )
