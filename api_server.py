"""FastAPI backend exposing the intraday screener, controlled automation, and broker APIs."""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import uvicorn
from dotenv import dotenv_values, set_key
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from broker import (
    AngelCredentials,
    AngelOneBroker,
    BrokerAuthError,
    BrokerError,
    BrokerOrderError,
    RateLimitError,
    TradeBookManager,
    load_credentials,
    save_credentials,
)
from risk_manager import (
    DAILY_MAX_LOSS,
    DAILY_PROFIT_TARGET,
    FNO_DAILY_MAX_LOSS,
    FNO_DAILY_PROFIT_TARGET,
    MAX_SL_PER_TRADE,
    MAX_TRADES_PER_DAY,
    FnoRiskConfig,
    RiskConfig,
    RiskSnapshot,
    SegmentType,
    WindowPreset,
    calculate_nse_charges,
    can_execute_trade,
    evaluate_risk_state,
    get_window_end_time,
    get_window_label,
    now_ist,
    round_to_tick,
)
from strategy import (
    NIFTY50_LIQUID_UNIVERSE,
    IndexTrend,
    StockEvaluation,
    UniversePreset,
    evaluate_index_trend,
    evaluate_stock_setup,
)


def get_or_create_mobile_token(env_path: str | Path = ".env") -> str:
    """Return existing MOBILE_API_TOKEN from env/.env or generate and persist one."""
    env_token = os.getenv("MOBILE_API_TOKEN", "").strip()
    if env_token:
        return env_token

    path = Path(env_path)
    if path.exists():
        file_token = str(dotenv_values(path).get("MOBILE_API_TOKEN") or "").strip()
        if file_token:
            return file_token

    token = secrets.token_urlsafe(24)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    set_key(str(path), "MOBILE_API_TOKEN", token)
    return token


MOBILE_API_TOKEN = get_or_create_mobile_token()


@dataclass
class AutomationSettings:
    """Controlled server-side background automation settings."""

    enabled: bool = False
    mode: Literal["PAPER", "LIVE"] = "PAPER"
    scan_interval_sec: int = 60
    ltp_interval_sec: int = 15
    auto_execute: bool = False
    auto_square_off_eod: bool = False
    capital: float = 50000.0
    max_sl: float = MAX_SL_PER_TRADE
    max_trades: int = MAX_TRADES_PER_DAY
    window_preset: WindowPreset = "morning"
    universe_preset: UniversePreset = "top15"
    custom_symbols: str = ""
    trailing_sl_enabled: bool = True
    nifty_trend_filter: bool = True


class AppRuntime:
    """Thread-safe shared server runtime state for mobile and automation workers."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.creds = load_credentials()
        self.broker = AngelOneBroker(self.creds)
        self.trade_book = TradeBookManager("trades_log.csv")
        self.evaluations: list[StockEvaluation] = []
        self.index_trend: IndexTrend | None = None
        self.ltp_map: dict[str, float] = {}
        self.trailing_sl_enabled: bool = True
        self.nifty_trend_filter: bool = True
        self.last_scan_time: str = "Not scanned yet"
        self.data_source: str = "-"
        self.executed_signal_candles: set[tuple[str, str]] = set()
        self.automation = AutomationSettings()
        self.last_scan_epoch: float = 0.0
        self.last_ltp_epoch: float = 0.0
        self.automation_state: str = "IDLE"
        self.activity_log: deque[str] = deque(maxlen=40)
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None

    def log_event(self, message: str) -> None:
        stamp = now_ist().strftime("%H:%M:%S")
        with self.lock:
            self.activity_log.appendleft(f"[{stamp}] {message}")


runtime = AppRuntime()

app = FastAPI(
    title="WinWin Intraday Mobile API",
    version="1.1.0",
    description="Authenticated HTTPS API with Controlled Automation for iPhone & Android",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_bearer_token(authorization: str | None = Header(default=None)) -> None:
    """Authenticate requests with Bearer MOBILE_API_TOKEN."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token in Authorization header.",
        )
    provided = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(provided, MOBILE_API_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid mobile API access token.",
        )


class ConnectBrokerRequest(BaseModel):
    api_key: str = Field(..., min_length=1)
    client_id: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    totp_secret: str = Field(..., min_length=1)


class RiskParams(BaseModel):
    mode: Literal["PAPER", "LIVE"] = "PAPER"
    capital: float = 50000.0
    max_sl: float = MAX_SL_PER_TRADE
    max_trades: int = MAX_TRADES_PER_DAY
    window_preset: WindowPreset = "morning"
    universe_preset: UniversePreset = "top15"
    custom_symbols: str = ""
    trailing_sl_enabled: bool = True
    nifty_trend_filter: bool = True
    window_preset: WindowPreset = "morning"
    universe_preset: UniversePreset = "top15"
    custom_symbols: str = ""


class ScanRequest(RiskParams):
    preview_outside_window: bool = False
    auto_execute: bool = False


class ExecuteRequest(RiskParams):
    symbol: str = Field(..., min_length=1)


class PanicExitRequest(BaseModel):
    mode: Literal["PAPER", "LIVE"] = "PAPER"
    capital: float = 50000.0
    max_sl: float = MAX_SL_PER_TRADE
    max_trades: int = MAX_TRADES_PER_DAY
    window_preset: WindowPreset = "morning"
    window_preset: WindowPreset = "morning"
    universe_preset: UniversePreset = "top15"
    custom_symbols: str = ""


class AutomationConfigRequest(RiskParams):
    enabled: bool = False
    scan_interval_sec: int = Field(default=60, ge=30, le=600)
    ltp_interval_sec: int = Field(default=15, ge=10, le=120)
    auto_execute: bool = False
    auto_square_off_eod: bool = False


class FnoAnalyzeRequest(BaseModel):
    mode: Literal["PAPER", "LIVE"] = "PAPER"
    underlying: Literal["NIFTY", "BANKNIFTY", "FINNIFTY"] = "NIFTY"
    instrument_type: Literal["OPTIDX", "FUTIDX"] = "OPTIDX"


class FnoExecuteRequest(BaseModel):
    mode: Literal["PAPER", "LIVE"] = "PAPER"
    tradingsymbol: str = Field(..., min_length=1)
    token: str = Field(..., min_length=1)
    qty: int = Field(..., ge=1)
    entry_price: float = Field(..., gt=0)
    sl_points: float = Field(default=15.0, ge=5.0, le=50.0)
    segment: Literal["NFO_OPTIONS", "NFO_FUTURES"] = "NFO_OPTIONS"
    window_preset: WindowPreset = "morning"
    risk_ack: bool = False


def _build_risk_cfg(
    capital: float,
    max_sl: float,
    max_trades: int,
    window_preset: WindowPreset = "morning",
) -> RiskConfig:
    return RiskConfig(
        capital=capital,
        daily_profit_target=DAILY_PROFIT_TARGET,
        daily_max_loss=DAILY_MAX_LOSS,
        max_sl_per_trade=max_sl,
        max_trades_per_day=max_trades,
        window_preset=window_preset,
    )


def _square_off_all_unlocked(
    mode: Literal["PAPER", "LIVE"],
    reason: str = "PANIC EXIT",
    segment: SegmentType | tuple[SegmentType, ...] | None = "EQ_INTRADAY",
) -> tuple[int, list[str]]:
    today_df = runtime.trade_book.get_today_trades(mode=mode, segment=segment)
    open_df = today_df[today_df["status"] == "OPEN"] if not today_df.empty else pd.DataFrame()
    if open_df.empty:
        return 0, ["No open positions to square off."]

    universe_lookup = {s["symbol"]: s for s in NIFTY50_LIQUID_UNIVERSE}
    nse_stocks = []
    nfo_contracts = []
    for _, r in open_df.iterrows():
        exch = str(r.get("exchange") or "NSE")
        if exch == "NFO":
            nfo_contracts.append({"tradingsymbol": str(r["tradingsymbol"]), "token": str(r["token"])})
        else:
            nse_stocks.append(
                universe_lookup.get(
                    str(r["symbol"]),
                    {"symbol": str(r["symbol"]), "token": str(r["token"]), "yahoo_symbol": f"{r['symbol']}.NS"},
                )
            )
    ltp_map: dict[str, float] = {}
    if nse_stocks:
        ltp_map.update(runtime.broker.fetch_batch_ltp(nse_stocks, require_smartapi=(mode == "LIVE")))
    if nfo_contracts:
        ltp_map.update(runtime.broker.fetch_nfo_ltp(nfo_contracts, require_smartapi=(mode == "LIVE")))
    runtime.ltp_map.update(ltp_map)

    closed_count = 0
    messages: list[str] = []
    for _, row in open_df.iterrows():
        sym = str(row["symbol"])
        tsym = str(row["tradingsymbol"])
        trade_id = str(row["trade_id"])
        ltp = float(ltp_map.get(sym) or ltp_map.get(tsym) or row["entry_price"])

        if mode == "PAPER":
            pnl = runtime.trade_book.close_trade(trade_id, ltp, reason)
            closed_count += 1
            messages.append(f"Squared off {tsym} (PAPER, {reason}) @ ₹{ltp:.2f} | Net P&L: ₹{pnl:+,.2f}")
        else:
            res = runtime.broker.square_off_live_trade(row.to_dict(), ltp_hint=ltp)
            pnl = runtime.trade_book.close_trade(trade_id, res["exit_price"], reason)
            closed_count += 1
            messages.append(f"Squared off {tsym} (LIVE, {reason}) @ ₹{res['exit_price']:.2f} | Net P&L: ₹{pnl:+,.2f}")

    return closed_count, messages


def _sync_and_snapshot_unlocked(
    mode: Literal["PAPER", "LIVE"],
    risk_cfg: RiskConfig,
    trailing_sl_enabled: bool | None = None,
) -> tuple[RiskSnapshot, dict[str, Any], list[dict[str, Any]]]:
    trail_on = runtime.trailing_sl_enabled if trailing_sl_enabled is None else trailing_sl_enabled
    events = runtime.trade_book.sync_open_trades_with_ltp(
        mode=mode,
        ltp_map=runtime.ltp_map,
        enable_trailing_sl=trail_on,
        broker=runtime.broker if mode == "LIVE" else None,
        segment="EQ_INTRADAY",
    )
    for ev in events:
        tail = f": ₹{ev['pnl']:+,.2f}" if ev.get("pnl") else ""
        runtime.log_event(f"{ev['symbol']} ({ev['reason']}){tail}")

    summary = runtime.trade_book.compute_today_summary(mode=mode, ltp_map=runtime.ltp_map, segment="EQ_INTRADAY")
    snapshot = evaluate_risk_state(
        realized_pnl=summary["realized_pnl"],
        unrealized_pnl=summary["unrealized_pnl"],
        trades_used=summary["trades_used"],
        open_positions_count=summary["open_positions_count"],
        config=risk_cfg,
    )
    if snapshot.should_auto_square_off and runtime.ltp_map:
        cnt, msgs = _square_off_all_unlocked(mode=mode, reason="KILL-SWITCH EXIT", segment="EQ_INTRADAY")
        if cnt > 0:
            runtime.log_event("Kill-switch auto square-off: " + " | ".join(msgs))
        summary = runtime.trade_book.compute_today_summary(mode=mode, ltp_map=runtime.ltp_map, segment="EQ_INTRADAY")
        snapshot = evaluate_risk_state(
            realized_pnl=summary["realized_pnl"],
            unrealized_pnl=summary["unrealized_pnl"],
            trades_used=summary["trades_used"],
            open_positions_count=summary["open_positions_count"],
            config=risk_cfg,
        )
    return snapshot, summary, events


def _execute_signal_unlocked(
    signal_eval: StockEvaluation,
    mode: Literal["PAPER", "LIVE"],
    risk_snapshot: RiskSnapshot,
    risk_cfg: RiskConfig,
    open_symbols: set[str],
    enforce_nifty_filter: bool | None = None,
) -> tuple[bool, str]:
    use_filter = runtime.nifty_trend_filter if enforce_nifty_filter is None else enforce_nifty_filter
    if use_filter and runtime.index_trend is not None and runtime.index_trend.error is None:
        if runtime.index_trend.regime == "BEARISH":
            return (
                False,
                f"Blocked by NIFTY 50 Trend Filter (NIFTY {runtime.index_trend.price} is BEARISH).",
            )

    eligible, reason = can_execute_trade(
        snapshot=risk_snapshot,
        symbol=signal_eval.symbol,
        open_symbols=open_symbols,
        qty=signal_eval.qty,
        sl_distance=signal_eval.sl_distance,
        config=risk_cfg,
    )
    if not eligible:
        return False, reason

    if mode == "PAPER":
        live_prices = runtime.broker.fetch_batch_ltp(
            [{"symbol": signal_eval.symbol, "token": signal_eval.token, "yahoo_symbol": f"{signal_eval.symbol}.NS"}],
            require_smartapi=False,
        )
        fill_px = live_prices.get(signal_eval.symbol, signal_eval.price)
        runtime.trade_book.record_entry(
            mode="PAPER",
            symbol=signal_eval.symbol,
            tradingsymbol=signal_eval.tradingsymbol,
            token=signal_eval.token,
            qty=signal_eval.qty,
            entry_price=fill_px,
            stop_loss=signal_eval.stop_loss,
            target=signal_eval.target,
            notes="Simulated fill at live market price",
        )
        msg = (
            f"PAPER BUY executed: {signal_eval.qty} x {signal_eval.symbol} @ ₹{fill_px:.2f} "
            f"(SL ₹{signal_eval.stop_loss:.2f} | Target ₹{signal_eval.target:.2f})"
        )
        runtime.log_event(msg)
        return True, msg

    order_res = runtime.broker.place_live_buy_with_protection(
        symbol=signal_eval.symbol,
        tradingsymbol=signal_eval.tradingsymbol,
        token=signal_eval.token,
        qty=signal_eval.qty,
        entry_price_hint=signal_eval.price,
        stop_loss=signal_eval.stop_loss,
        target=signal_eval.target,
    )
    runtime.trade_book.record_entry(
        mode="LIVE",
        symbol=signal_eval.symbol,
        tradingsymbol=signal_eval.tradingsymbol,
        token=signal_eval.token,
        qty=signal_eval.qty,
        entry_price=order_res["fill_price"],
        stop_loss=signal_eval.stop_loss,
        target=signal_eval.target,
        entry_order_id=order_res["entry_order_id"],
        sl_order_id=order_res["sl_order_id"],
        target_order_id=order_res["target_order_id"],
        notes=order_res["notes"],
    )
    msg = (
        f"LIVE BUY placed: {signal_eval.qty} x {signal_eval.tradingsymbol} @ ₹{order_res['fill_price']:.2f} "
        f"(Order ID: {order_res['entry_order_id']})"
    )
    runtime.log_event(msg)
    return True, msg


def _fetch_index_trend_safe(mode: Literal["PAPER", "LIVE"], index_name: str = "NIFTY") -> IndexTrend:
    try:
        df, _ = runtime.broker.fetch_index_spot_candles(index_name, use_smartapi=(mode == "LIVE"), days_back=5)
        return evaluate_index_trend(df, index_name=index_name)
    except Exception:
        return evaluate_index_trend(pd.DataFrame(), index_name=index_name)


def _perform_network_scan(
    mode: Literal["PAPER", "LIVE"],
    risk_cfg: RiskConfig,
    universe_preset: UniversePreset = "top15",
    custom_symbols: str = "",
) -> tuple[list[StockEvaluation], dict[str, float], set[str], str | None]:
    """Fetch 5-minute candles & batch LTPs without holding runtime.lock."""
    use_smartapi = mode == "LIVE"
    stocks = runtime.broker.resolve_universe_tokens(
        universe_preset=universe_preset,
        custom_symbols=custom_symbols,
    )
    ltp_map = runtime.broker.fetch_batch_ltp(stocks, require_smartapi=use_smartapi)

    evaluations: list[StockEvaluation] = []
    sources_used: set[str] = set()
    rate_limit_msg: str | None = None

    for stock in stocks:
        try:
            candles, src = runtime.broker.fetch_5m_candles(stock, use_smartapi=use_smartapi, days_back=5)
            sources_used.add(src)
            ev = evaluate_stock_setup(
                symbol=stock["symbol"],
                tradingsymbol=stock["tradingsymbol"],
                token=stock["token"],
                candles=candles,
                capital=risk_cfg.capital,
                max_sl_rupees=risk_cfg.max_sl_per_trade,
                live_ltp=ltp_map.get(stock["symbol"]),
            )
            evaluations.append(ev)
        except RateLimitError as exc:
            rate_limit_msg = str(exc)
            break
        except Exception as exc:
            err_ev = evaluate_stock_setup(
                symbol=stock["symbol"],
                tradingsymbol=stock["tradingsymbol"],
                token=stock["token"],
                candles=pd.DataFrame(),
            )
            evaluations.append(StockEvaluation(**{**err_ev.__dict__, "error": str(exc)}))

    return evaluations, ltp_map, sources_used, rate_limit_msg


def _refresh_open_ltps(mode: Literal["PAPER", "LIVE"]) -> dict[str, float]:
    """Fetch batch LTPs for today's open positions outside runtime.lock."""
    with runtime.lock:
        today_df = runtime.trade_book.get_today_trades(mode=mode)
        open_rows = today_df[today_df["status"] == "OPEN"] if not today_df.empty else pd.DataFrame()
        if open_rows.empty:
            return {}
        open_stocks = [
            {"symbol": str(r["symbol"]), "token": str(r["token"]), "yahoo_symbol": f"{r['symbol']}.NS"}
            for _, r in open_rows.iterrows()
        ]
    return runtime.broker.fetch_batch_ltp(open_stocks, require_smartapi=(mode == "LIVE"))


def run_automation_tick() -> None:
    """Execute one controlled background automation cycle (LTP sync + optional scan/auto-exec)."""
    with runtime.lock:
        auto = runtime.automation
        if not auto.enabled:
            runtime.automation_state = "IDLE"
            return
        mode = auto.mode
        cfg = _build_risk_cfg(auto.capital, auto.max_sl, auto.max_trades, auto.window_preset)
        u_preset = auto.universe_preset
        c_syms = auto.custom_symbols
        scan_interval = auto.scan_interval_sec
        ltp_interval = auto.ltp_interval_sec
        auto_exec = auto.auto_execute
        auto_eod = auto.auto_square_off_eod
        last_ltp = runtime.last_ltp_epoch
        last_scan = runtime.last_scan_epoch
        broker_ok = runtime.broker.is_connected

    now_ep = time.monotonic()

    # 1. Refresh LTPs for open trades & check SL/Target/Kill-Switch
    if now_ep - last_ltp >= ltp_interval:
        try:
            fresh_ltps = _refresh_open_ltps(mode=mode)
            with runtime.lock:
                runtime.last_ltp_epoch = time.monotonic()
                if fresh_ltps:
                    runtime.ltp_map.update(fresh_ltps)
                snapshot, summary, _ = _sync_and_snapshot_unlocked(mode=mode, risk_cfg=cfg)
                if (
                    auto_eod
                    and summary["open_positions_count"] > 0
                    and now_ist().time() > get_window_end_time(cfg.window_preset)
                ):
                    cnt, msgs = _square_off_all_unlocked(mode=mode, reason="WINDOW EXIT")
                    if cnt > 0:
                        runtime.log_event("Window-end auto exit: " + " | ".join(msgs))
        except Exception as exc:
            runtime.log_event(f"LTP monitor warning: {exc}")

    # 2. Evaluate risk state before scanning
    with runtime.lock:
        snapshot, summary, _ = _sync_and_snapshot_unlocked(mode=mode, risk_cfg=cfg)
        if snapshot.system_status != "ACTIVE":
            runtime.automation_state = snapshot.system_status
            return
        if not snapshot.can_trade:
            runtime.automation_state = f"PAUSED ({snapshot.status_reason})"
            return
        if mode == "LIVE" and not broker_ok:
            runtime.automation_state = "WAITING FOR SMARTAPI LOGIN"
            return
        runtime.automation_state = "RUNNING"

    # 3. Periodic scan + controlled Auto-Execute
    if now_ep - last_scan >= scan_interval:
        try:
            evaluations, ltp_map, sources_used, rate_limit_msg = _perform_network_scan(
                mode=mode,
                risk_cfg=cfg,
                universe_preset=u_preset,
                custom_symbols=c_syms,
            )
            with runtime.lock:
                runtime.last_scan_epoch = time.monotonic()
                runtime.ltp_map.update(ltp_map)
                runtime.evaluations = evaluations
                runtime.last_scan_time = now_ist().strftime("%H:%M:%S IST")
                runtime.data_source = ", ".join(sorted(sources_used)) if sources_used else "-"
                buy_signals = [e for e in evaluations if e.is_buy_signal]
                if rate_limit_msg:
                    runtime.log_event(f"Auto-scan rate limit: {rate_limit_msg}")
                else:
                    runtime.log_event(f"Auto-scan: {len(evaluations)} stocks, {len(buy_signals)} BUY signal(s).")

                if auto_exec and buy_signals:
                    snapshot, summary, _ = _sync_and_snapshot_unlocked(mode=mode, risk_cfg=cfg)
                    for sig in buy_signals:
                        if snapshot.trades_used >= cfg.max_trades_per_day:
                            break
                        sig_key = (sig.symbol, sig.candle_time)
                        if sig.symbol in summary["open_symbols"] or sig_key in runtime.executed_signal_candles:
                            continue
                        ok, msg = _execute_signal_unlocked(sig, mode, snapshot, cfg, summary["open_symbols"])
                        if ok:
                            runtime.executed_signal_candles.add(sig_key)
                            snapshot, summary, _ = _sync_and_snapshot_unlocked(mode=mode, risk_cfg=cfg)
                        else:
                            runtime.log_event(f"Skipped {sig.symbol}: {msg}")
        except Exception as exc:
            runtime.log_event(f"Auto-scan error: {exc}")


def _automation_loop() -> None:
    while not runtime._stop_event.is_set():
        try:
            run_automation_tick()
        except Exception as exc:
            runtime.log_event(f"Automation loop error: {exc}")
        runtime._stop_event.wait(timeout=3.0)


@app.on_event("startup")
def start_automation_worker() -> None:
    if runtime._worker is None or not runtime._worker.is_alive():
        runtime._stop_event.clear()
        runtime._worker = threading.Thread(target=_automation_loop, name="winwin-autopilot", daemon=True)
        runtime._worker.start()


@app.on_event("shutdown")
def stop_automation_worker() -> None:
    runtime._stop_event.set()


def _serialize_state_unlocked(
    mode: Literal["PAPER", "LIVE"],
    risk_cfg: RiskConfig,
    notice: str | None = None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    snapshot, summary, sync_events = _sync_and_snapshot_unlocked(mode=mode, risk_cfg=risk_cfg)
    all_events = (events or []) + sync_events

    today_df = runtime.trade_book.get_today_trades(mode=mode, segment="EQ_INTRADAY")
    trades: list[dict[str, Any]] = []
    if not today_df.empty:
        for _, row in today_df.iterrows():
            sym = str(row["symbol"])
            st_val = str(row["status"])
            entry_px = float(row["entry_price"])
            qty = int(row["qty"])
            ltp = runtime.ltp_map.get(sym)
            if st_val == "OPEN":
                current_px = ltp if (ltp is not None and ltp > 0) else entry_px
                gross = round((current_px - entry_px) * qty, 2)
                chg = calculate_nse_charges("EQ_INTRADAY", entry_px * qty, current_px * qty)["total"]
                pnl_val = round(gross - chg, 2)
            else:
                current_px = float(row["exit_price"]) if pd.notna(row["exit_price"]) else entry_px
                chg = float(row["charges"]) if pd.notna(row["charges"]) else 0.0
                pnl_val = float(row["realized_pnl"]) if pd.notna(row["realized_pnl"]) else 0.0

            trades.append(
                {
                    "trade_id": str(row["trade_id"]),
                    "timestamp": str(row["timestamp"]),
                    "mode": str(row["mode"]),
                    "symbol": sym,
                    "qty": qty,
                    "entry_price": entry_px,
                    "stop_loss": float(row["stop_loss"]),
                    "target": float(row["target"]),
                    "ltp_or_exit": round(current_px, 2),
                    "charges": chg,
                    "pnl": pnl_val,
                    "status": st_val,
                    "exit_reason": str(row["exit_reason"] or ""),
                    "entry_order_id": str(row["entry_order_id"] or ""),
                }
            )

    signals = [e.__dict__ for e in runtime.evaluations if e.is_buy_signal]
    diagnostics = [e.__dict__ for e in runtime.evaluations]

    return {
        "time_ist": now_ist().strftime("%H:%M:%S IST"),
        "mode": mode,
        "broker_connected": runtime.broker.is_connected,
        "broker_profile": runtime.broker.profile_name,
        "last_scan_time": runtime.last_scan_time,
        "data_source": runtime.data_source,
        "index_trend": runtime.index_trend.__dict__ if runtime.index_trend else None,
        "guardrails": {
            "daily_profit_target": DAILY_PROFIT_TARGET,
            "daily_max_loss": DAILY_MAX_LOSS,
            "max_sl_per_trade": MAX_SL_PER_TRADE,
            "max_trades_per_day": MAX_TRADES_PER_DAY,
            "active_window": get_window_label(risk_cfg.window_preset),
            "window_preset": risk_cfg.window_preset,
        },
        "risk_snapshot": {
            "realized_pnl": snapshot.realized_pnl,
            "gross_realized_pnl": summary["gross_realized_pnl"],
            "total_charges": summary["total_charges"],
            "unrealized_pnl": snapshot.unrealized_pnl,
            "net_pnl": snapshot.net_pnl,
            "trades_used": snapshot.trades_used,
            "max_trades": snapshot.max_trades,
            "open_positions_count": snapshot.open_positions_count,
            "system_status": snapshot.system_status,
            "status_reason": snapshot.status_reason,
            "within_active_window": snapshot.within_active_window,
            "can_trade": snapshot.can_trade,
            "goal_progress_pct": snapshot.goal_progress_pct,
        },
        "automation": {
            "enabled": runtime.automation.enabled,
            "mode": runtime.automation.mode,
            "scan_interval_sec": runtime.automation.scan_interval_sec,
            "ltp_interval_sec": runtime.automation.ltp_interval_sec,
            "auto_execute": runtime.automation.auto_execute,
            "auto_square_off_eod": runtime.automation.auto_square_off_eod,
            "window_preset": runtime.automation.window_preset,
            "universe_preset": runtime.automation.universe_preset,
            "custom_symbols": runtime.automation.custom_symbols,
            "trailing_sl_enabled": runtime.trailing_sl_enabled,
            "nifty_trend_filter": runtime.nifty_trend_filter,
            "state": runtime.automation_state,
            "activity_log": list(runtime.activity_log),
        },
        "signals": signals,
        "diagnostics": diagnostics,
        "trades": trades,
        "events": all_events,
        "notice": notice,
    }


@app.get("/", response_class=HTMLResponse)
def serve_mobile_web() -> HTMLResponse:
    """Serve the standalone mobile/tablet browser app (iPhone Safari & Android Chrome)."""
    html_path = Path(__file__).resolve().parent / "mobile_web.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/manifest.webmanifest")
def serve_manifest() -> JSONResponse:
    """Serve Web App Manifest so iOS/Android 'Add to Home Screen' runs full-screen."""
    return JSONResponse(
        content={
            "name": "WinWin Intraday Trader",
            "short_name": "WinWin Trader",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#F8FAFC",
            "theme_color": "#0F172A",
            "icons": [
                {
                    "src": "/icon.svg",
                    "sizes": "any",
                    "type": "image/svg+xml",
                    "purpose": "any maskable",
                }
            ],
        },
        media_type="application/manifest+json",
    )


@app.get("/icon.svg")
def serve_icon() -> Response:
    """Serve crisp SVG app icon for Home Screen shortcuts."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">'
        '<rect width="128" height="128" rx="28" fill="#0F172A"/>'
        '<path d="M26 84 L50 56 L70 70 L102 36" fill="none" stroke="#10B981" stroke-width="10" stroke-linecap="round" stroke-linejoin="round"/>'
        '<circle cx="102" cy="36" r="7" fill="#10B981"/>'
        "</svg>"
    )
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/health")
def health() -> dict[str, str]:
    """Unauthenticated health check for cloud/tunnel monitors."""
    return {"status": "ok", "time_ist": now_ist().strftime("%Y-%m-%d %H:%M:%S IST")}


@app.get("/api/state", dependencies=[Depends(require_bearer_token)])
def get_state(
    mode: Literal["PAPER", "LIVE"] = Query("PAPER"),
    capital: float = Query(50000.0),
    max_sl: float = Query(MAX_SL_PER_TRADE),
    max_trades: int = Query(MAX_TRADES_PER_DAY),
    window_preset: WindowPreset = Query("morning"),
    trailing_sl_enabled: bool = Query(True),
    nifty_trend_filter: bool = Query(True),
) -> dict[str, Any]:
    """Fetch current metrics, automation status, signals, diagnostics, and today's trade book."""
    with runtime.lock:
        runtime.trailing_sl_enabled = trailing_sl_enabled
        runtime.nifty_trend_filter = nifty_trend_filter
        cfg = _build_risk_cfg(capital, max_sl, max_trades, window_preset)
        return _serialize_state_unlocked(mode=mode, risk_cfg=cfg)


@app.post("/api/automation", dependencies=[Depends(require_bearer_token)])
def configure_automation(req: AutomationConfigRequest) -> dict[str, Any]:
    """Start, update, or stop the server-side Controlled Auto-Pilot loop."""
    with runtime.lock:
        if req.enabled and req.mode == "LIVE" and not runtime.broker.is_connected:
            raise HTTPException(
                status_code=400,
                detail="Connect Angel One SmartAPI before enabling Auto-Pilot in LIVE mode.",
            )
        runtime.trailing_sl_enabled = req.trailing_sl_enabled
        runtime.nifty_trend_filter = req.nifty_trend_filter
        cfg = _build_risk_cfg(req.capital, req.max_sl, req.max_trades, req.window_preset)
        runtime.automation = AutomationSettings(
            enabled=req.enabled,
            mode=req.mode,
            scan_interval_sec=req.scan_interval_sec,
            ltp_interval_sec=req.ltp_interval_sec,
            auto_execute=req.auto_execute,
            auto_square_off_eod=req.auto_square_off_eod,
            capital=cfg.capital,
            max_sl=cfg.max_sl_per_trade,
            max_trades=cfg.max_trades_per_day,
            window_preset=req.window_preset,
            universe_preset=req.universe_preset,
            custom_symbols=req.custom_symbols,
            trailing_sl_enabled=req.trailing_sl_enabled,
            nifty_trend_filter=req.nifty_trend_filter,
        )
        runtime.last_scan_epoch = 0.0
        runtime.last_ltp_epoch = 0.0
        status_label = (
            f"Auto-Pilot ENABLED ({req.mode}, {req.universe_preset}, {req.window_preset}, scan={req.scan_interval_sec}s)"
            if req.enabled
            else "Auto-Pilot STOPPED"
        )
        runtime.automation_state = "STARTING" if req.enabled else "IDLE"
        runtime.log_event(status_label)
        return _serialize_state_unlocked(mode=req.mode, risk_cfg=cfg, notice=status_label)


@app.post("/api/broker/connect", dependencies=[Depends(require_bearer_token)])
def connect_broker(req: ConnectBrokerRequest) -> dict[str, Any]:
    """Save Angel One credentials to .env and authenticate SmartAPI session."""
    with runtime.lock:
        new_creds = AngelCredentials(
            api_key=req.api_key,
            client_id=req.client_id,
            password=req.password,
            totp_secret=req.totp_secret,
        )
        save_credentials(new_creds)
        runtime.creds = new_creds
        try:
            runtime.broker.connect(new_creds)
        except BrokerAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        runtime.log_event(f"Angel One SmartAPI connected ({runtime.broker.profile_name}).")
        return {
            "connected": True,
            "profile_name": runtime.broker.profile_name,
            "message": f"Connected as {runtime.broker.profile_name}. Saved to .env.",
        }


@app.post("/api/broker/disconnect", dependencies=[Depends(require_bearer_token)])
def disconnect_broker() -> dict[str, Any]:
    """Terminate the active Angel One SmartAPI session."""
    with runtime.lock:
        runtime.broker.disconnect()
        if runtime.automation.mode == "LIVE":
            runtime.automation.enabled = False
            runtime.automation_state = "IDLE"
        runtime.log_event("Angel One SmartAPI disconnected.")
        return {"connected": False, "message": "Angel One SmartAPI disconnected."}


@app.post("/api/scan", dependencies=[Depends(require_bearer_token)])
def scan_market(req: ScanRequest) -> dict[str, Any]:
    """Run 5-minute scan and optionally auto-execute signals within guardrails."""
    with runtime.lock:
        cfg = _build_risk_cfg(req.capital, req.max_sl, req.max_trades, req.window_preset)
        snapshot, summary, events = _sync_and_snapshot_unlocked(mode=req.mode, risk_cfg=cfg)
        if not snapshot.can_trade and not req.preview_outside_window:
            raise HTTPException(status_code=400, detail=snapshot.status_reason)
        if req.mode == "LIVE" and not runtime.broker.is_connected:
            raise HTTPException(status_code=400, detail="Connect Angel One SmartAPI before scanning in LIVE mode.")

    try:
        idx_trend = _fetch_index_trend_safe(mode=req.mode, index_name="NIFTY")
        evaluations, ltp_map, sources_used, rate_limit_msg = _perform_network_scan(
            mode=req.mode,
            risk_cfg=cfg,
            universe_preset=req.universe_preset,
            custom_symbols=req.custom_symbols,
        )
    except (BrokerAuthError, RateLimitError, BrokerError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    with runtime.lock:
        runtime.trailing_sl_enabled = req.trailing_sl_enabled
        runtime.nifty_trend_filter = req.nifty_trend_filter
        runtime.index_trend = idx_trend
        runtime.last_scan_epoch = time.monotonic()
        runtime.ltp_map.update(ltp_map)
        runtime.evaluations = evaluations
        runtime.last_scan_time = now_ist().strftime("%H:%M:%S IST")
        runtime.data_source = ", ".join(sorted(sources_used)) if sources_used else "-"

        notices: list[str] = []
        if rate_limit_msg:
            notices.append(f"Rate limit paused scan: {rate_limit_msg}")
        else:
            buy_count = sum(1 for e in evaluations if e.is_buy_signal)
            tag = "Read-only preview" if (req.preview_outside_window and not snapshot.within_active_window) else "Scan complete"
            notices.append(f"{tag}: {len(evaluations)} stocks evaluated, {buy_count} BUY signal(s).")
        runtime.log_event(notices[0])

        snapshot, summary, sync_events = _sync_and_snapshot_unlocked(mode=req.mode, risk_cfg=cfg)
        events = events + sync_events

        if req.auto_execute and snapshot.can_trade and not req.preview_outside_window:
            for sig in [e for e in evaluations if e.is_buy_signal]:
                if snapshot.trades_used >= cfg.max_trades_per_day:
                    break
                sig_key = (sig.symbol, sig.candle_time)
                if sig.symbol in summary["open_symbols"] or sig_key in runtime.executed_signal_candles:
                    continue
                try:
                    ok, msg = _execute_signal_unlocked(sig, req.mode, snapshot, cfg, summary["open_symbols"])
                    notices.append(msg)
                    if ok:
                        runtime.executed_signal_candles.add(sig_key)
                        snapshot, summary, _ = _sync_and_snapshot_unlocked(mode=req.mode, risk_cfg=cfg)
                except (BrokerAuthError, RateLimitError, BrokerOrderError, BrokerError) as exc:
                    notices.append(f"{sig.symbol}: {exc}")

        return _serialize_state_unlocked(
            mode=req.mode,
            risk_cfg=cfg,
            notice=" | ".join(notices),
            events=events,
        )


@app.post("/api/execute", dependencies=[Depends(require_bearer_token)])
def execute_trade(req: ExecuteRequest) -> dict[str, Any]:
    """Execute one evaluated BUY signal for the requested symbol."""
    with runtime.lock:
        cfg = _build_risk_cfg(req.capital, req.max_sl, req.max_trades, req.window_preset)
        snapshot, summary, events = _sync_and_snapshot_unlocked(mode=req.mode, risk_cfg=cfg)
        sig = next(
            (e for e in runtime.evaluations if e.symbol.upper() == req.symbol.upper() and e.is_buy_signal),
            None,
        )
        if sig is None:
            raise HTTPException(
                status_code=400,
                detail=f"No active BUY signal found for {req.symbol}. Run a scan first.",
            )

        try:
            ok, msg = _execute_signal_unlocked(sig, req.mode, snapshot, cfg, summary["open_symbols"])
        except (BrokerAuthError, RateLimitError, BrokerOrderError, BrokerError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not ok:
            raise HTTPException(status_code=400, detail=msg)

        runtime.executed_signal_candles.add((sig.symbol, sig.candle_time))
        return _serialize_state_unlocked(mode=req.mode, risk_cfg=cfg, notice=msg, events=events)


@app.post("/api/refresh-ltps", dependencies=[Depends(require_bearer_token)])
def refresh_ltps(req: RiskParams) -> dict[str, Any]:
    """Refresh live LTPs for today's open positions and evaluate SL/Target hits."""
    cfg = _build_risk_cfg(req.capital, req.max_sl, req.max_trades, req.window_preset)
    try:
        fresh = _refresh_open_ltps(mode=req.mode)
    except (BrokerAuthError, RateLimitError, BrokerError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    with runtime.lock:
        runtime.last_ltp_epoch = time.monotonic()
        if fresh:
            runtime.ltp_map.update(fresh)
        return _serialize_state_unlocked(mode=req.mode, risk_cfg=cfg, notice="Live LTPs refreshed.")


@app.post("/api/panic-exit", dependencies=[Depends(require_bearer_token)])
def panic_exit(req: PanicExitRequest) -> dict[str, Any]:
    """Immediately square off all open intraday equity positions in the active mode."""
    with runtime.lock:
        cfg = _build_risk_cfg(req.capital, req.max_sl, req.max_trades, req.window_preset)
        try:
            closed_cnt, msgs = _square_off_all_unlocked(mode=req.mode, reason="PANIC EXIT", segment="EQ_INTRADAY")
        except (BrokerAuthError, RateLimitError, BrokerOrderError, BrokerError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if closed_cnt > 0:
            runtime.log_event("Panic exit: " + " | ".join(msgs))
        if closed_cnt == 0 and msgs == ["No open positions to square off."]:
            return _serialize_state_unlocked(mode=req.mode, risk_cfg=cfg, notice=msgs[0])
        return _serialize_state_unlocked(mode=req.mode, risk_cfg=cfg, notice=" | ".join(msgs))


def _serialize_fno_unlocked(mode: Literal["PAPER", "LIVE"], window_preset: WindowPreset = "morning", notice: str | None = None) -> dict[str, Any]:
    fno_segs: tuple[SegmentType, ...] = ("NFO_OPTIONS", "NFO_FUTURES")
    runtime.trade_book.sync_open_trades_with_ltp(
        mode=mode,
        ltp_map=runtime.ltp_map,
        enable_trailing_sl=runtime.trailing_sl_enabled,
        broker=runtime.broker if mode == "LIVE" else None,
        segment=fno_segs,
    )
    summary = runtime.trade_book.compute_today_summary(mode=mode, ltp_map=runtime.ltp_map, segment=fno_segs)
    snap = evaluate_risk_state(
        realized_pnl=summary["realized_pnl"],
        unrealized_pnl=summary["unrealized_pnl"],
        trades_used=summary["trades_used"],
        open_positions_count=summary["open_positions_count"],
        config=RiskConfig(window_preset=window_preset),
    )
    if summary["net_pnl"] <= -FNO_DAILY_MAX_LOSS or summary["realized_pnl"] <= -FNO_DAILY_MAX_LOSS:
        status_txt = "KILL-SWITCH LOCKED"
        can_trade_fno = False
    elif summary["realized_pnl"] >= FNO_DAILY_PROFIT_TARGET:
        status_txt = "GOAL LOCKED"
        can_trade_fno = False
    else:
        status_txt = "ACTIVE"
        can_trade_fno = snap.within_active_window and summary["trades_used"] < 2

    df = runtime.trade_book.get_today_trades(mode=mode, segment=fno_segs)
    trades = []
    if not df.empty:
        for _, r in df.iterrows():
            tsym = str(r["tradingsymbol"])
            epx = float(r["entry_price"])
            qty = int(r["qty"])
            seg = str(r["segment"] or "NFO_OPTIONS")
            st_val = str(r["status"])
            ltp = runtime.ltp_map.get(tsym) or runtime.ltp_map.get(str(r["symbol"]))
            if st_val == "OPEN":
                px = ltp if (ltp and ltp > 0) else epx
                gross = (px - epx) * qty
                chg = calculate_nse_charges(seg, epx * qty, px * qty)["total"]  # type: ignore[arg-type]
                pnl_val = round(gross - chg, 2)
            else:
                px = float(r["exit_price"]) if pd.notna(r["exit_price"]) else epx
                chg = float(r["charges"]) if pd.notna(r["charges"]) else 0.0
                pnl_val = float(r["realized_pnl"]) if pd.notna(r["realized_pnl"]) else 0.0
            trades.append(
                {
                    "trade_id": str(r["trade_id"]),
                    "timestamp": str(r["timestamp"]),
                    "segment": seg,
                    "tradingsymbol": tsym,
                    "qty": qty,
                    "entry_price": epx,
                    "stop_loss": float(r["stop_loss"]),
                    "target": float(r["target"]),
                    "ltp_or_exit": round(px, 2),
                    "charges": chg,
                    "pnl": pnl_val,
                    "status": st_val,
                    "exit_reason": str(r["exit_reason"] or ""),
                }
            )
    return {
        "mode": mode,
        "status": status_txt,
        "can_trade": can_trade_fno,
        "within_active_window": snap.within_active_window,
        "daily_target": FNO_DAILY_PROFIT_TARGET,
        "daily_max_loss": FNO_DAILY_MAX_LOSS,
        "summary": {k: v for k, v in summary.items() if k != "open_symbols"},
        "trades": trades,
        "notice": notice,
    }


@app.get("/api/fno/state", dependencies=[Depends(require_bearer_token)])
def get_fno_state(
    mode: Literal["PAPER", "LIVE"] = Query("PAPER"),
    window_preset: WindowPreset = Query("morning"),
) -> dict[str, Any]:
    with runtime.lock:
        return _serialize_fno_unlocked(mode=mode, window_preset=window_preset)


@app.post("/api/fno/analyze", dependencies=[Depends(require_bearer_token)])
def analyze_fno(req: FnoAnalyzeRequest) -> dict[str, Any]:
    df, _ = runtime.broker.fetch_index_spot_candles(req.underlying, use_smartapi=(req.mode == "LIVE"), days_back=5)
    trend = evaluate_index_trend(df, index_name=req.underlying)
    opt_side = "PE" if trend.regime == "BEARISH" else "CE"
    contracts = runtime.broker.get_fno_contracts(
        underlying=req.underlying,
        instrument_type=req.instrument_type,
        spot_price=trend.price if trend.price > 0 else None,
        option_type=opt_side,
        max_strikes=5,
    )
    ltps: dict[str, float] = {}
    if runtime.broker.is_connected and contracts:
        ltps = runtime.broker.fetch_nfo_ltp(contracts, require_smartapi=False)
        with runtime.lock:
            runtime.ltp_map.update(ltps)
    enriched = [{**c, "ltp": ltps.get(c["tradingsymbol"], 0.0)} for c in contracts]
    return {"trend": trend.__dict__, "contracts": enriched}


@app.post("/api/fno/execute", dependencies=[Depends(require_bearer_token)])
def execute_fno(req: FnoExecuteRequest) -> dict[str, Any]:
    if not req.risk_ack:
        raise HTTPException(status_code=400, detail="Please confirm F&O risk acknowledgement before executing.")
    with runtime.lock:
        fno_state = _serialize_fno_unlocked(mode=req.mode, window_preset=req.window_preset)
        if not fno_state["can_trade"]:
            raise HTTPException(status_code=400, detail=f"F&O execution blocked ({fno_state['status']}).")
        sl_px = round_to_tick(max(0.05, req.entry_price - req.sl_points))
        tgt_px = round_to_tick(req.entry_price + 2.0 * req.sl_points)
        if req.mode == "PAPER":
            fill_px = runtime.ltp_map.get(req.tradingsymbol, req.entry_price)
            runtime.trade_book.record_entry(
                mode="PAPER",
                symbol=req.tradingsymbol,
                tradingsymbol=req.tradingsymbol,
                token=req.token,
                qty=req.qty,
                entry_price=fill_px,
                stop_loss=round_to_tick(max(0.05, fill_px - req.sl_points)),
                target=round_to_tick(fill_px + 2.0 * req.sl_points),
                notes="F&O Paper (Mobile)",
                segment=req.segment,
                exchange="NFO",
            )
            return _serialize_fno_unlocked(mode=req.mode, window_preset=req.window_preset, notice=f"F&O PAPER BUY: {req.qty} x {req.tradingsymbol}")
        res = runtime.broker.place_live_buy_with_protection(
            symbol=req.tradingsymbol,
            tradingsymbol=req.tradingsymbol,
            token=req.token,
            qty=req.qty,
            entry_price_hint=req.entry_price,
            stop_loss=sl_px,
            target=tgt_px,
            exchange="NFO",
        )
        runtime.trade_book.record_entry(
            mode="LIVE",
            symbol=req.tradingsymbol,
            tradingsymbol=req.tradingsymbol,
            token=req.token,
            qty=req.qty,
            entry_price=res["fill_price"],
            stop_loss=sl_px,
            target=tgt_px,
            entry_order_id=res["entry_order_id"],
            sl_order_id=res["sl_order_id"],
            target_order_id=res["target_order_id"],
            notes=res["notes"],
            segment=req.segment,
            exchange="NFO",
        )
        return _serialize_fno_unlocked(mode=req.mode, window_preset=req.window_preset, notice=f"F&O LIVE BUY: {req.tradingsymbol}")


@app.post("/api/fno/panic-exit", dependencies=[Depends(require_bearer_token)])
def panic_exit_fno(req: PanicExitRequest) -> dict[str, Any]:
    with runtime.lock:
        cnt, msgs = _square_off_all_unlocked(mode=req.mode, reason="PANIC EXIT", segment=("NFO_OPTIONS", "NFO_FUTURES"))
        return _serialize_fno_unlocked(mode=req.mode, window_preset=req.window_preset, notice=" | ".join(msgs))


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    print(f"\nWinWin Mobile API Token (MOBILE_API_TOKEN in .env): {MOBILE_API_TOKEN}\n")
    uvicorn.run("api_server:app", host="0.0.0.0", port=port, reload=False)
