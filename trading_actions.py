"""Shared execution, scanning, and risk-sync helpers for Streamlit pages."""

from __future__ import annotations

import time
from typing import Any

import pandas as pd
import streamlit as st

from broker import (
    AngelOneBroker,
    BrokerAuthError,
    BrokerError,
    BrokerOrderError,
    RateLimitError,
    TradeBookManager,
)
from notifier import notify_async
import risk_manager
from risk_manager import (
    RiskConfig,
    RiskSnapshot,
    SegmentType,
    can_execute_trade,
    evaluate_risk_state,
)
from strategy import (
    NIFTY50_LIQUID_UNIVERSE,
    IndexTrend,
    StockEvaluation,
    UniversePreset,
    evaluate_index_trend,
    evaluate_stock_setup,
)


def log_autopilot_event(msg: str) -> None:
    """Prepend a timestamped event to the Streamlit Auto-Pilot activity log."""
    stamp = risk_manager.now_ist().strftime("%H:%M:%S")
    logs: list[str] = st.session_state.get("autopilot_log", [])
    logs.insert(0, f"[{stamp}] {msg}")
    st.session_state.autopilot_log = logs[:30]


def execute_single_trade(
    signal_eval: StockEvaluation,
    mode: str,
    risk_snapshot: RiskSnapshot,
    risk_cfg: RiskConfig,
    open_symbols: set[str],
    index_trend: IndexTrend | None = None,
    enforce_nifty_filter: bool = True,
) -> tuple[bool, str]:
    """Validate guardrails (including NIFTY Trend Filter) and execute Paper or Live BUY."""
    if enforce_nifty_filter and index_trend is not None and index_trend.error is None:
        if index_trend.regime == "BEARISH":
            return (
                False,
                f"Blocked by NIFTY 50 Trend Filter (NIFTY {index_trend.price} is BEARISH; avoid counter-trend equity BUY).",
            )

    broker: AngelOneBroker = st.session_state.broker
    trade_book: TradeBookManager = st.session_state.trade_book

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
        live_prices = broker.fetch_batch_ltp(
            [{"symbol": signal_eval.symbol, "token": signal_eval.token, "yahoo_symbol": f"{signal_eval.symbol}.NS"}],
            require_smartapi=False,
        )
        fill_px = live_prices.get(signal_eval.symbol, signal_eval.price)
        trade_book.record_entry(
            mode="PAPER",
            symbol=signal_eval.symbol,
            tradingsymbol=signal_eval.tradingsymbol,
            token=signal_eval.token,
            qty=signal_eval.qty,
            entry_price=fill_px,
            stop_loss=signal_eval.stop_loss,
            target=signal_eval.target,
            notes="Simulated fill at live market price",
            segment="EQ_INTRADAY",
            exchange="NSE",
        )
        msg = (
            f"PAPER BUY executed: {signal_eval.qty} x {signal_eval.symbol} @ ₹{fill_px:.2f} "
            f"(SL ₹{signal_eval.stop_loss:.2f} | Target ₹{signal_eval.target:.2f})"
        )
        notify_async(f"[WinWin] {msg}", cfg=st.session_state.get("tg_config"))
        return True, msg

    try:
        order_res = broker.place_live_buy_with_protection(
            symbol=signal_eval.symbol,
            tradingsymbol=signal_eval.tradingsymbol,
            token=signal_eval.token,
            qty=signal_eval.qty,
            entry_price_hint=signal_eval.price,
            stop_loss=signal_eval.stop_loss,
            target=signal_eval.target,
            exchange="NSE",
        )
        trade_book.record_entry(
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
            segment="EQ_INTRADAY",
            exchange="NSE",
        )
        msg = (
            f"LIVE BUY placed: {signal_eval.qty} x {signal_eval.tradingsymbol} @ ₹{order_res['fill_price']:.2f} "
            f"(Order ID: {order_res['entry_order_id']})"
        )
        notify_async(f"[WinWin] {msg}", cfg=st.session_state.get("tg_config"))
        return True, msg
    except (BrokerAuthError, RateLimitError, BrokerOrderError, BrokerError) as exc:
        return False, str(exc)


def execute_panic_square_off(
    mode: str,
    segment: SegmentType | tuple[SegmentType, ...] | None = "EQ_INTRADAY",
) -> tuple[int, list[str]]:
    """Square off every OPEN position for today in the active mode and segment(s)."""
    broker: AngelOneBroker = st.session_state.broker
    trade_book: TradeBookManager = st.session_state.trade_book

    today_df = trade_book.get_today_trades(mode=mode, segment=segment)
    open_df = today_df[today_df["status"] == "OPEN"] if not today_df.empty else pd.DataFrame()
    if open_df.empty:
        return 0, ["No open positions to square off."]

    universe_lookup = {s["symbol"]: s for s in NIFTY50_LIQUID_UNIVERSE}
    nse_open = []
    nfo_open = []
    for _, r in open_df.iterrows():
        exch = str(r.get("exchange") or "NSE")
        if exch == "NFO":
            nfo_open.append({"tradingsymbol": str(r["tradingsymbol"]), "token": str(r["token"])})
        else:
            nse_open.append(
                universe_lookup.get(
                    str(r["symbol"]),
                    {"symbol": str(r["symbol"]), "token": str(r["token"]), "yahoo_symbol": f"{r['symbol']}.NS"},
                )
            )

    ltp_map: dict[str, float] = {}
    if nse_open:
        ltp_map.update(broker.fetch_batch_ltp(nse_open, require_smartapi=(mode == "LIVE")))
    if nfo_open:
        ltp_map.update(broker.fetch_nfo_ltp(nfo_open, require_smartapi=(mode == "LIVE")))
    st.session_state.ltp_map.update(ltp_map)

    closed_count = 0
    messages: list[str] = []

    for _, row in open_df.iterrows():
        sym = str(row["symbol"])
        tsym = str(row["tradingsymbol"])
        trade_id = str(row["trade_id"])
        ltp = float(ltp_map.get(sym) or ltp_map.get(tsym) or row["entry_price"])

        if mode == "PAPER":
            pnl = trade_book.close_trade(trade_id, ltp, "PANIC EXIT")
            closed_count += 1
            messages.append(f"Squared off {tsym} (PAPER) @ ₹{ltp:.2f} | Net P&L: ₹{pnl:+,.2f}")
        else:
            try:
                res = broker.square_off_live_trade(row.to_dict(), ltp_hint=ltp)
                pnl = trade_book.close_trade(trade_id, res["exit_price"], "PANIC EXIT")
                closed_count += 1
                messages.append(
                    f"Squared off {tsym} (LIVE, Exit ID {res['exit_order_id']}) @ ₹{res['exit_price']:.2f} | Net P&L: ₹{pnl:+,.2f}"
                )
            except Exception as exc:
                messages.append(f"Failed to square off {tsym}: {exc}")

    return closed_count, messages


def run_market_scan(
    mode: str,
    risk_cfg: RiskConfig,
    allow_outside_window_preview: bool = False,
    show_progress: bool = True,
) -> None:
    """Scan selected NSE stock universe on 5m candles and evaluate NIFTY 50 Index Trend."""
    broker: AngelOneBroker = st.session_state.broker
    use_smartapi = mode == "LIVE"
    u_preset: UniversePreset = st.session_state.get("universe_preset", "top15")
    c_syms: str = st.session_state.get("custom_symbols", "")
    stocks = broker.resolve_universe_tokens(universe_preset=u_preset, custom_symbols=c_syms)

    # Fetch NIFTY 50 spot trend first
    try:
        idx_candles, _ = broker.fetch_index_spot_candles("NIFTY", use_smartapi=use_smartapi, days_back=5)
        st.session_state.index_trend = evaluate_index_trend(idx_candles, index_name="NIFTY")
    except Exception as exc:
        st.session_state.index_trend = evaluate_index_trend(pd.DataFrame(), index_name="NIFTY")
        log_autopilot_event(f"NIFTY trend check note: {exc}")

    ltp_map = broker.fetch_batch_ltp(stocks, require_smartapi=use_smartapi)
    st.session_state.ltp_map.update(ltp_map)

    evaluations: list[StockEvaluation] = []
    sources_used: set[str] = set()
    rate_limit_hit = False

    progress = st.progress(0, text=f"Scanning {len(stocks)} NSE stocks...") if show_progress else None
    for idx, stock in enumerate(stocks):
        if progress is not None:
            progress.progress((idx + 1) / len(stocks), text=f"Scanning {stock['symbol']} ({idx + 1}/{len(stocks)})...")
        try:
            candles, src = broker.fetch_5m_candles(stock, use_smartapi=use_smartapi, days_back=5)
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
            rate_limit_hit = True
            st.session_state.last_notice = ("error", f"Rate limit paused scan at {stock['symbol']}: {exc}")
            log_autopilot_event(f"Rate limit paused scan at {stock['symbol']}: {exc}")
            break
        except Exception as exc:
            err_ev = evaluate_stock_setup(
                symbol=stock["symbol"],
                tradingsymbol=stock["tradingsymbol"],
                token=stock["token"],
                candles=pd.DataFrame(),
            )
            evaluations.append(StockEvaluation(**{**err_ev.__dict__, "error": str(exc)}))
    if progress is not None:
        progress.empty()

    st.session_state.evaluations = evaluations
    st.session_state.last_scan_time = risk_manager.now_ist().strftime("%H:%M:%S IST")
    st.session_state.autopilot_last_scan_ep = time.monotonic()
    st.session_state.data_source = ", ".join(sorted(sources_used)) if sources_used else "-"

    if not rate_limit_hit:
        mode_tag = "Read-only preview" if allow_outside_window_preview else "Scan complete"
        buy_signals = [e for e in evaluations if e.is_buy_signal]
        buy_count = len(buy_signals)
        summary_msg = f"{mode_tag}: {len(evaluations)} stocks evaluated, {buy_count} BUY signal(s) found."
        st.session_state.last_notice = ("info", summary_msg)
        log_autopilot_event(summary_msg)
        notified: set[tuple[str, str]] = st.session_state.get("notified_signal_candles", set())
        for sig in buy_signals:
            sig_key = (sig.symbol, sig.candle_time)
            if sig_key not in notified:
                notified.add(sig_key)
                notify_async(
                    f"[WinWin BUY SIGNAL] {sig.symbol}\nEntry: ₹{sig.price:.2f} | SL: ₹{sig.stop_loss:.2f} | Target: ₹{sig.target:.2f} | Qty: {sig.qty}",
                    cfg=st.session_state.get("tg_config"),
                )
        st.session_state.notified_signal_candles = notified


def build_risk_snapshot(mode: str, risk_cfg: RiskConfig) -> tuple[RiskSnapshot, dict[str, Any]]:
    """Sync open equity trades (Trailing SL + Live OCO + SL/Target) and compute RiskSnapshot."""
    broker: AngelOneBroker = st.session_state.broker
    trade_book: TradeBookManager = st.session_state.trade_book
    ltp_map: dict[str, float] = st.session_state.ltp_map
    trailing_on: bool = bool(st.session_state.get("trailing_sl_enabled", True))

    closed_events = trade_book.sync_open_trades_with_ltp(
        mode=mode,
        ltp_map=ltp_map,
        enable_trailing_sl=trailing_on,
        broker=broker if mode == "LIVE" else None,
        segment="EQ_INTRADAY",
    )
    for ev in closed_events:
        msg = f"{ev['symbol']}: {ev['reason']}" + (f" (Net ₹{ev['pnl']:+,.2f})" if ev["pnl"] else "")
        st.toast(msg)
        log_autopilot_event(msg)
        notify_async(f"[WinWin] {msg}", cfg=st.session_state.get("tg_config"))

    summary = trade_book.compute_today_summary(mode=mode, ltp_map=ltp_map, segment="EQ_INTRADAY")
    snapshot = evaluate_risk_state(
        realized_pnl=summary["realized_pnl"],
        unrealized_pnl=summary["unrealized_pnl"],
        trades_used=summary["trades_used"],
        open_positions_count=summary["open_positions_count"],
        config=risk_cfg,
    )

    if snapshot.should_auto_square_off and ltp_map:
        closed_cnt, msgs = execute_panic_square_off(mode=mode, segment="EQ_INTRADAY")
        if closed_cnt > 0:
            st.session_state.last_notice = (
                "error",
                "Kill-switch auto square-off triggered! " + " | ".join(msgs),
            )
            log_autopilot_event("Kill-switch auto square-off: " + " | ".join(msgs))
            summary = trade_book.compute_today_summary(mode=mode, ltp_map=ltp_map, segment="EQ_INTRADAY")
            snapshot = evaluate_risk_state(
                realized_pnl=summary["realized_pnl"],
                unrealized_pnl=summary["unrealized_pnl"],
                trades_used=summary["trades_used"],
                open_positions_count=summary["open_positions_count"],
                config=risk_cfg,
            )

    return snapshot, summary
