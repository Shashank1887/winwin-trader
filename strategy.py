"""Intraday Nifty 50 screener indicators, BUY/SELL setups, and 5-day backtester."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time as dt_time
from typing import Literal

import numpy as np
import pandas as pd

from risk_manager import (
    IST,
    MAX_SL_PER_TRADE,
    calculate_nse_charges,
    calculate_position_sizing,
)

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
    side: Literal["BUY", "SELL", "WAIT"] = "WAIT"
    is_sell_signal: bool = False
    cond_price_below_vwap: bool = False
    cond_ema_bearish: bool = False
    cond_sell_rsi_and_volume: bool = False

    @property
    def has_signal(self) -> bool:
        return self.is_buy_signal or self.is_sell_signal


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


def _prepare_indicator_df(candles: pd.DataFrame) -> pd.DataFrame:
    """Clean OHLCV DataFrame and compute VWAP, EMA9, EMA21, RSI14, and ATR14."""
    df = candles.sort_values("timestamp").reset_index(drop=True).copy()
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)
    if df.empty:
        return df

    ts = pd.to_datetime(df["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(IST)
    else:
        ts = ts.dt.tz_convert(IST)
    df["timestamp"] = ts

    df["vwap"] = compute_intraday_vwap(df)
    df["ema_9"] = compute_ema(df["close"], 9)
    df["ema_21"] = compute_ema(df["close"], 21)
    df["rsi_14"] = compute_rsi(df["close"], 14)
    df["atr_14"] = compute_atr(df, 14)
    return df


def _evaluate_closed_candle_at_idx(
    symbol: str,
    tradingsymbol: str,
    token: str,
    df: pd.DataFrame,
    eval_idx: int,
    capital: float = 50000.0,
    max_sl_rupees: float = MAX_SL_PER_TRADE,
    entry_price_override: float | None = None,
) -> StockEvaluation:
    """Evaluate BUY and SELL conditions strictly on completed candle `df.iloc[eval_idx]`."""
    closed = df.iloc[eval_idx]
    prev = df.iloc[eval_idx - 1]

    vol_start = max(0, eval_idx - 20)
    prior_volumes = df["volume"].iloc[vol_start:eval_idx]
    avg_vol_20 = float(prior_volumes.mean()) if len(prior_volumes) > 0 else float(df["volume"].mean())
    closed_vol = float(closed["volume"])
    vol_ratio = round(closed_vol / avg_vol_20, 2) if avg_vol_20 > 0 else 0.0

    closed_close = float(closed["close"])
    entry_px = (
        float(entry_price_override)
        if (entry_price_override is not None and entry_price_override > 0)
        else closed_close
    )
    vwap = float(closed["vwap"]) if pd.notna(closed["vwap"]) else closed_close
    ema_9 = float(closed["ema_9"])
    ema_21 = float(closed["ema_21"])
    prev_ema_9 = float(prev["ema_9"])
    prev_ema_21 = float(prev["ema_21"])
    rsi_14 = float(closed["rsi_14"]) if pd.notna(closed["rsi_14"]) else 50.0
    atr_14 = float(closed["atr_14"]) if pd.notna(closed["atr_14"]) else closed_close * 0.004

    has_vol_expansion = (avg_vol_20 > 0) and (closed_vol > 1.2 * avg_vol_20)

    # BUY setup (closed candle iloc[-2])
    cond_price_above_vwap = closed_close > vwap
    cond_ema_bullish = ema_9 > ema_21
    cond_buy_rsi_and_vol = (52.0 <= rsi_14 <= 70.0) and has_vol_expansion

    # SELL (Short) setup (closed candle iloc[-2])
    cond_price_below_vwap = closed_close < vwap
    cond_ema_bearish = ema_9 < ema_21
    cond_sell_rsi_and_vol = (30.0 <= rsi_14 <= 48.0) and has_vol_expansion

    swing_window = df.iloc[max(0, eval_idx - 2) : eval_idx + 1]
    min_sl_dist = max(entry_px * 0.0025, 0.50)

    recent_low = float(swing_window["low"].min())
    buy_atr_sl = entry_px - max(atr_14, min_sl_dist)
    raw_buy_sl = min(recent_low, buy_atr_sl)
    if raw_buy_sl >= entry_px - min_sl_dist:
        raw_buy_sl = entry_px - min_sl_dist
    buy_sizing = calculate_position_sizing(
        entry_price=entry_px,
        stop_loss_price=raw_buy_sl,
        capital=capital,
        max_sl_rupees=max_sl_rupees,
        side="BUY",
    )

    recent_high = float(swing_window["high"].max())
    sell_atr_sl = entry_px + max(atr_14, min_sl_dist)
    raw_sell_sl = max(recent_high, sell_atr_sl)
    if raw_sell_sl <= entry_px + min_sl_dist:
        raw_sell_sl = entry_px + min_sl_dist
    sell_sizing = calculate_position_sizing(
        entry_price=entry_px,
        stop_loss_price=raw_sell_sl,
        capital=capital,
        max_sl_rupees=max_sl_rupees,
        side="SELL",
    )

    is_buy = bool(cond_price_above_vwap and cond_ema_bullish and cond_buy_rsi_and_vol and buy_sizing.qty >= 1)
    is_sell = bool(
        (not is_buy) and cond_price_below_vwap and cond_ema_bearish and cond_sell_rsi_and_vol and sell_sizing.qty >= 1
    )

    if is_sell:
        side: Literal["BUY", "SELL", "WAIT"] = "SELL"
        active_sizing = sell_sizing
        rsi_vol_flag = cond_sell_rsi_and_vol
    elif is_buy:
        side = "BUY"
        active_sizing = buy_sizing
        rsi_vol_flag = cond_buy_rsi_and_vol
    else:
        side = "WAIT"
        active_sizing = sell_sizing if (cond_price_below_vwap and cond_ema_bearish) else buy_sizing
        rsi_vol_flag = cond_buy_rsi_and_vol or cond_sell_rsi_and_vol

    ts = pd.to_datetime(closed["timestamp"])
    if ts.tzinfo is None:
        ts = ts.tz_localize(IST)
    else:
        ts = ts.tz_convert(IST)

    return StockEvaluation(
        symbol=symbol,
        tradingsymbol=tradingsymbol,
        token=token,
        candle_time=ts.strftime("%Y-%m-%d %H:%M IST"),
        price=round(entry_px, 2),
        vwap=round(vwap, 2),
        ema_9=round(ema_9, 2),
        ema_21=round(ema_21, 2),
        prev_ema_9=round(prev_ema_9, 2),
        prev_ema_21=round(prev_ema_21, 2),
        rsi_14=round(rsi_14, 2),
        volume=round(closed_vol, 0),
        avg_volume_20=round(avg_vol_20, 0),
        volume_ratio=vol_ratio,
        cond_price_above_vwap=cond_price_above_vwap,
        cond_ema_bullish_crossover=cond_ema_bullish,
        cond_rsi_and_volume=rsi_vol_flag,
        is_buy_signal=is_buy,
        stop_loss=active_sizing.stop_loss,
        target=active_sizing.target,
        qty=active_sizing.qty,
        sl_distance=active_sizing.sl_distance,
        expected_profit=active_sizing.target_profit_rupees,
        error=None,
        side=side,
        is_sell_signal=is_sell,
        cond_price_below_vwap=cond_price_below_vwap,
        cond_ema_bearish=cond_ema_bearish,
        cond_sell_rsi_and_volume=cond_sell_rsi_and_vol,
    )


def evaluate_stock_setup(
    symbol: str,
    tradingsymbol: str,
    token: str,
    candles: pd.DataFrame,
    capital: float = 50000.0,
    max_sl_rupees: float = MAX_SL_PER_TRADE,
    live_ltp: float | None = None,
) -> StockEvaluation:
    """Evaluate intraday BUY & SELL setups strictly on the last completed candle (iloc[-2])."""
    required_cols = {"timestamp", "open", "high", "low", "close", "volume"}
    if candles is None or candles.empty or not required_cols.issubset(candles.columns):
        return _error_eval(symbol, tradingsymbol, token, "Insufficient or malformed candle data.")

    df = _prepare_indicator_df(candles)
    if len(df) < 23:
        return _error_eval(
            symbol, tradingsymbol, token, f"Need at least 23 5-min candles, got {len(df)}."
        )

    # Always evaluate the last completed candle (iloc[-2]), never the live forming candle (iloc[-1])
    return _evaluate_closed_candle_at_idx(
        symbol=symbol,
        tradingsymbol=tradingsymbol,
        token=token,
        df=df,
        eval_idx=len(df) - 2,
        capital=capital,
        max_sl_rupees=max_sl_rupees,
        entry_price_override=live_ltp,
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
    if len(df) < 23:
        return IndexTrend(
            index_name=index_name,
            candle_time="-",
            price=0.0,
            ema_9=0.0,
            ema_21=0.0,
            rsi_14=0.0,
            regime="NEUTRAL",
            recommended_fno_action="WAIT",
            error=f"Need >= 23 index candles, got {len(df)}.",
        )

    df["ema_9"] = compute_ema(df["close"], 9)
    df["ema_21"] = compute_ema(df["close"], 21)
    df["rsi_14"] = compute_rsi(df["close"], 14)

    curr = df.iloc[-2]
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


def _simulate_candle_exit(
    df: pd.DataFrame,
    start_idx: int,
    trade_date: object,
    side: str,
    entry_px: float,
    sl_px: float,
    target_px: float,
    sl_dist: float,
    sl_rupees: float,
    target_rupees: float,
) -> tuple[float, float, str, str]:
    """Walk intraday candles of the signal date to find SL, Target, or EOD close."""
    eod_cutoff = dt_time(15, 15)
    for j in range(start_idx, len(df)):
        bar = df.iloc[j]
        bar_ts = bar["timestamp"]
        if bar_ts.date() != trade_date:
            break
        hi = float(bar["high"])
        lo = float(bar["low"])
        close_px = float(bar["close"])

        if side == "BUY":
            if lo <= sl_px:
                return -sl_rupees, sl_px, "SL HIT", bar_ts.strftime("%Y-%m-%d %H:%M")
            if hi >= target_px:
                return target_rupees, target_px, "TARGET HIT", bar_ts.strftime("%Y-%m-%d %H:%M")
            is_last_bar = (j + 1 >= len(df)) or (df.iloc[j + 1]["timestamp"].date() != trade_date)
            if bar_ts.time() >= eod_cutoff or is_last_bar:
                gross = round(sl_rupees * ((close_px - entry_px) / sl_dist), 2)
                return gross, close_px, "EOD EXIT", bar_ts.strftime("%Y-%m-%d %H:%M")
        else:
            if hi >= sl_px:
                return -sl_rupees, sl_px, "SL HIT", bar_ts.strftime("%Y-%m-%d %H:%M")
            if lo <= target_px:
                return target_rupees, target_px, "TARGET HIT", bar_ts.strftime("%Y-%m-%d %H:%M")
            is_last_bar = (j + 1 >= len(df)) or (df.iloc[j + 1]["timestamp"].date() != trade_date)
            if bar_ts.time() >= eod_cutoff or is_last_bar:
                gross = round(sl_rupees * ((entry_px - close_px) / sl_dist), 2)
                return gross, close_px, "EOD EXIT", bar_ts.strftime("%Y-%m-%d %H:%M")

    return 0.0, entry_px, "EOD EXIT", df.iloc[min(start_idx, len(df) - 1)]["timestamp"].strftime("%Y-%m-%d %H:%M")


def run_strategy_backtest(
    candles_by_symbol: dict[str, pd.DataFrame],
    capital: float = 50000.0,
    sl_rupees: float = 400.0,
    target_rupees: float = 800.0,
    days: int = 5,
) -> dict[str, object]:
    """Run the exact BUY & SELL closed-candle strategy over the last N trading days."""
    trades: list[dict[str, object]] = []
    entry_start = dt_time(9, 20)
    entry_end = dt_time(14, 45)
    scanned_stocks = 0

    for symbol, raw_df in candles_by_symbol.items():
        if raw_df is None or raw_df.empty:
            continue
        df = _prepare_indicator_df(raw_df)
        if len(df) < 24:
            continue
        scanned_stocks += 1

        unique_dates = sorted(df["timestamp"].dt.date.unique())
        active_dates = set(unique_dates[-max(1, int(days)) :])

        idx = 22
        last_idx = len(df) - 2  # evaluate only completed candles with at least 1 next bar to enter
        while idx <= last_idx:
            bar_ts = df.iloc[idx]["timestamp"]
            bar_date = bar_ts.date()
            if bar_date not in active_dates or not (entry_start <= bar_ts.time() <= entry_end):
                idx += 1
                continue

            next_bar = df.iloc[idx + 1]
            if next_bar["timestamp"].date() != bar_date:
                idx += 1
                continue

            next_open = float(next_bar["open"]) if float(next_bar["open"]) > 0 else float(df.iloc[idx]["close"])
            ev = _evaluate_closed_candle_at_idx(
                symbol=symbol,
                tradingsymbol=f"{symbol}-EQ",
                token="",
                df=df,
                eval_idx=idx,
                capital=capital,
                max_sl_rupees=sl_rupees,
                entry_price_override=next_open,
            )
            if not ev.has_signal or ev.sl_distance <= 0:
                idx += 1
                continue

            # Resolve 1:2 RR target scaled to fixed ₹400 SL / ₹800 Target
            rr_ratio = target_rupees / sl_rupees if sl_rupees > 0 else 2.0
            if ev.side == "BUY":
                tgt_px = ev.price + rr_ratio * ev.sl_distance
            else:
                tgt_px = max(0.05, ev.price - rr_ratio * ev.sl_distance)

            gross_pnl, exit_px, outcome, exit_time = _simulate_candle_exit(
                df=df,
                start_idx=idx + 1,
                trade_date=bar_date,
                side=ev.side,
                entry_px=ev.price,
                sl_px=ev.stop_loss,
                target_px=tgt_px,
                sl_dist=ev.sl_distance,
                sl_rupees=sl_rupees,
                target_rupees=target_rupees,
            )

            eff_qty = max(1, round(sl_rupees / ev.sl_distance))
            buy_v, sell_v = (
                (ev.price * eff_qty, exit_px * eff_qty)
                if ev.side == "BUY"
                else (exit_px * eff_qty, ev.price * eff_qty)
            )
            charges = calculate_nse_charges("EQ_INTRADAY", buy_value=buy_v, sell_value=sell_v)["total"]
            net_pnl = round(gross_pnl - charges, 2)

            trades.append(
                {
                    "Stock": symbol,
                    "Side": ev.side,
                    "Signal Candle": ev.candle_time,
                    "Entry": ev.price,
                    "Exit": round(exit_px, 2),
                    "Exit Time": exit_time,
                    "Outcome": outcome,
                    "Gross P&L": round(gross_pnl, 2),
                    "Charges": charges,
                    "Net P&L": net_pnl,
                }
            )

            # Skip ahead until next bar after exit_time on same day (no overlapping trades per stock)
            exit_ts = pd.to_datetime(exit_time).tz_localize(IST)
            while idx <= last_idx and df.iloc[idx]["timestamp"] <= exit_ts:
                idx += 1

    def _summarize(label: str, subset: list[dict[str, object]]) -> dict[str, object]:
        total = len(subset)
        wins = sum(1 for t in subset if float(t["Net P&L"]) > 0)
        losses = sum(1 for t in subset if float(t["Net P&L"]) <= 0)
        win_rate = round((wins / total) * 100.0, 1) if total > 0 else 0.0
        gross_sum = round(sum(float(t["Gross P&L"]) for t in subset), 2)
        chg_sum = round(sum(float(t["Charges"]) for t in subset), 2)
        net_sum = round(sum(float(t["Net P&L"]) for t in subset), 2)
        return {
            "Setup": label,
            "Signals": total,
            "Wins": wins,
            "Losses": losses,
            "Win Rate (%)": win_rate,
            "Gross P&L": gross_sum,
            "Charges": chg_sum,
            "Net P&L": net_sum,
        }

    buy_trades = [t for t in trades if t["Side"] == "BUY"]
    sell_trades = [t for t in trades if t["Side"] == "SELL"]
    summary_rows = [
        _summarize("BUY (Long)", buy_trades),
        _summarize("SELL (Short)", sell_trades),
        _summarize("TOTAL (All)", trades),
    ]
    return {
        "stocks_tested": scanned_stocks,
        "days": days,
        "summary_rows": summary_rows,
        "trades": trades,
        "total_signals": len(trades),
        "win_rate_pct": summary_rows[-1]["Win Rate (%)"],
        "net_pnl": summary_rows[-1]["Net P&L"],
    }
