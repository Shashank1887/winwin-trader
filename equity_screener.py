"""Equity Intraday Screener & Execution page."""

from __future__ import annotations

import time

import pandas as pd
import streamlit as st

from broker import AngelOneBroker, TradeBookManager
import risk_manager
from risk_manager import (
    DAILY_PROFIT_TARGET,
    RiskConfig,
    calculate_nse_charges,
    can_execute_trade,
    get_window_end_time,
    get_window_label,
)
from strategy import IndexTrend, StockEvaluation
from trading_actions import (
    build_risk_snapshot,
    execute_panic_square_off,
    execute_single_trade,
    log_autopilot_event,
    run_market_scan,
)

risk_cfg: RiskConfig = st.session_state.active_risk_cfg
broker: AngelOneBroker = st.session_state.broker
trade_book: TradeBookManager = st.session_state.trade_book
trailing_on: bool = bool(st.session_state.get("trailing_sl_enabled", True))
nifty_filter_on: bool = bool(st.session_state.get("nifty_trend_filter", True))
index_trend: IndexTrend | None = st.session_state.get("index_trend")

# Header & Prominent Mode Switch
head_left, head_right = st.columns([3, 2], vertical_alignment="center")
with head_left:
    st.title("Intraday Trading Assistant & Screener", anchor=False)
    st.caption(
        f"NSE Equity 5m Screener • Window: {get_window_label(risk_cfg.window_preset)} • Clock: {risk_manager.now_ist().strftime('%H:%M:%S IST')}"
    )
    if index_trend and index_trend.error is None:
        badge_color = "green" if index_trend.regime == "BULLISH" else ("red" if index_trend.regime == "BEARISH" else "gray")
        st.badge(
            f"NIFTY 50 Spot: {index_trend.price:,.1f} ({index_trend.regime} | RSI {index_trend.rsi_14})",
            icon=":material/trending_up:",
            color=badge_color,
        )
with head_right:
    with st.container(border=True):
        paper_mode = st.toggle(
            "PAPER TRADING (Simulated orders at live prices)",
            key="paper_mode",
        )
        mode = "PAPER" if paper_mode else "LIVE"
        if paper_mode:
            st.badge("PAPER MODE ACTIVE — Virtual P&L logged to trades_log.csv", icon=":material/science:", color="green")
        else:
            st.badge("LIVE MODE ACTIVE — Real orders via Angel One API", icon=":material/warning:", color="red")

risk_snapshot, summary = build_risk_snapshot(mode=mode, risk_cfg=risk_cfg)

if mode == "LIVE" and not broker.is_connected:
    st.error(
        "LIVE TRADING selected, but Angel One SmartAPI is not connected. Enter credentials in the sidebar and click 'Save to .env & Connect'.",
        icon=":material/gpp_maybe:",
    )

# Guardrail Lock Banners
if risk_snapshot.system_status == "GOAL LOCKED":
    st.success(
        f"Goal Achieved! Daily net P&L (after charges) reached +₹{risk_snapshot.realized_pnl:,.2f}. Trading is auto-locked for today.",
        icon=":material/emoji_events:",
    )
elif risk_snapshot.system_status == "KILL-SWITCH LOCKED":
    st.error(
        f"Kill-Switch Activated! {risk_snapshot.status_reason} All new orders are immediately locked.",
        icon=":material/gpp_bad:",
    )
elif not risk_snapshot.within_active_window:
    st.warning(
        f"Outside Selected Window ({get_window_label(risk_cfg.window_preset)}). Scanning and new trade execution are paused.",
        icon=":material/schedule:",
    )

if nifty_filter_on and index_trend and index_trend.error is None and index_trend.regime == "BEARISH":
    st.warning(
        f"NIFTY 50 Trend Filter Active: Index is BEARISH ({index_trend.price:,.1f} < EMA21 {index_trend.ema_21:,.1f}). New equity BUY entries are blocked to protect capital.",
        icon=":material/shield:",
    )

if st.session_state.last_notice:
    level, msg = st.session_state.last_notice
    if level == "success":
        st.success(msg, icon=":material/check_circle:")
    elif level == "error":
        st.error(msg, icon=":material/error:")
    else:
        st.info(msg, icon=":material/info:")
    st.session_state.last_notice = None

# Top Row: 4 Clean Metric Cards
c1, c2, c3, c4 = st.columns(4)
pnl_delta = 0 if risk_snapshot.net_pnl == 0 else f"₹{risk_snapshot.net_pnl:+,.2f}"
c1.metric(
    "Today's Net P&L",
    f"₹{risk_snapshot.net_pnl:+,.2f}",
    delta=pnl_delta,
    delta_description=f"Gross ₹{summary['gross_realized_pnl']:+,.2f} | Charges ₹{summary['total_charges']:,.2f}",
    border=True,
    icon=":material/account_balance_wallet:",
)

goal_delta = 0 if risk_snapshot.goal_progress_pct == 0 else f"{risk_snapshot.goal_progress_pct:.0f}%"
c2.metric(
    "Daily Goal Progress",
    f"₹{risk_snapshot.realized_pnl:,.0f} / ₹{DAILY_PROFIT_TARGET:,.0f}",
    delta=goal_delta,
    delta_description="Net after brokerage & taxes",
    border=True,
    icon=":material/flag:",
)

c3.metric(
    "Trades Used",
    f"{risk_snapshot.trades_used} / {risk_snapshot.max_trades}",
    delta=f"{risk_snapshot.open_positions_count} open",
    delta_color="off",
    border=True,
    icon=":material/swap_horiz:",
)

status_sub = "Ready" if risk_snapshot.can_trade else risk_snapshot.status_reason
c4.metric(
    "System Status",
    risk_snapshot.system_status,
    delta=status_sub[:38],
    delta_color="off",
    border=True,
    icon=":material/shield:",
)

st.space("small")

# Controlled Auto-Pilot Panel
with st.container(border=True):
    ap_left, ap_right = st.columns([3, 2], vertical_alignment="center")
    with ap_left:
        st.subheader("Controlled Auto-Pilot", anchor=False)
        st.caption(
            f"Auto-monitors Trailing SL, Live OCO & Kill-Switch every 15s and scans during {get_window_label(risk_cfg.window_preset)}."
        )
    with ap_right:
        with st.container(horizontal=True, horizontal_alignment="right"):
            autopilot_on = st.toggle(
                "Enable Auto-Pilot",
                key="autopilot_enabled",
                disabled=(mode == "LIVE" and not broker.is_connected),
            )

    ap_c1, ap_c2, ap_c3 = st.columns(3, vertical_alignment="center")
    with ap_c1:
        interval_choice = st.segmented_control(
            "Scan interval",
            options=["30s", "60s", "180s", "300s"],
            default="60s",
            key="autopilot_interval",
        )
    with ap_c2:
        st.checkbox(
            "Auto-Execute BUY signals",
            key="auto_execute",
            disabled=not risk_snapshot.can_trade,
        )
    with ap_c3:
        auto_eod = st.checkbox(
            "Auto square-off after window end",
            key="autopilot_auto_eod",
        )

    if autopilot_on:
        interval_sec = int((interval_choice or "60s").removesuffix("s"))

        @st.fragment(run_every=15)
        def _autopilot_fragment() -> None:
            need_full_rerun = False
            today_df = trade_book.get_today_trades(mode=mode, segment="EQ_INTRADAY")
            open_rows = today_df[today_df["status"] == "OPEN"] if not today_df.empty else pd.DataFrame()
            if not open_rows.empty:
                open_stocks = [
                    {"symbol": str(r["symbol"]), "token": str(r["token"]), "yahoo_symbol": f"{r['symbol']}.NS"}
                    for _, r in open_rows.iterrows()
                ]
                try:
                    fresh_ltps = broker.fetch_batch_ltp(open_stocks, require_smartapi=(mode == "LIVE"))
                    st.session_state.ltp_map.update(fresh_ltps)
                except Exception as exc:
                    log_autopilot_event(f"LTP monitor error: {exc}")

            snap_now, sum_now = build_risk_snapshot(mode=mode, risk_cfg=risk_cfg)
            if sum_now["open_positions_count"] != summary["open_positions_count"]:
                need_full_rerun = True

            window_end_t = get_window_end_time(risk_cfg.window_preset)
            if auto_eod and sum_now["open_positions_count"] > 0 and risk_manager.now_ist().time() > window_end_t:
                cnt, msgs = execute_panic_square_off(mode=mode, segment="EQ_INTRADAY")
                if cnt > 0:
                    log_autopilot_event("Window-end square-off: " + " | ".join(msgs))
                    need_full_rerun = True

            if snap_now.can_trade and (mode == "PAPER" or broker.is_connected):
                if time.monotonic() - st.session_state.autopilot_last_scan_ep >= interval_sec:
                    run_market_scan(mode=mode, risk_cfg=risk_cfg, show_progress=False)
                    need_full_rerun = True

            st.caption(
                f"Auto-Pilot: {'ACTIVE' if snap_now.can_trade else snap_now.system_status} • "
                f"Trailing SL {'ON' if trailing_on else 'OFF'} • "
                f"LTP 15s • Scan {interval_sec}s • {risk_manager.now_ist().strftime('%H:%M:%S IST')}"
            )
            if need_full_rerun:
                st.rerun()

        _autopilot_fragment()

    if st.session_state.autopilot_log:
        ap_log_exp = st.expander("Auto-Pilot activity log", on_change="rerun")
        if ap_log_exp.open:
            with ap_log_exp:
                st.code("\n".join(st.session_state.autopilot_log[:15]), language=None)

st.space("small")

# Middle Section: Actionable Signals
with st.container(border=True):
    st.subheader("Actionable signals", anchor=False)
    with st.container(horizontal=True, vertical_alignment="center"):
        scan_enabled = risk_snapshot.can_trade and (mode == "PAPER" or broker.is_connected)
        if st.button(
            "Scan stocks now",
            icon=":material/radar:",
            key="scan_btn",
            disabled=not scan_enabled,
            help=f"Runs 5-minute VWAP / EMA(9,21) / RSI(14) / Volume scan during {get_window_label(risk_cfg.window_preset)}.",
        ):
            run_market_scan(mode=mode, risk_cfg=risk_cfg, allow_outside_window_preview=False)
            st.rerun()

        if not risk_snapshot.within_active_window:
            preview_enabled = mode == "PAPER" or broker.is_connected
            if st.button(
                "Preview candles (read-only)",
                icon=":material/visibility:",
                key="preview_scan_btn",
                disabled=not preview_enabled,
                help="Inspect live/latest 5-minute indicator state outside selected window without allowing execution.",
            ):
                run_market_scan(mode=mode, risk_cfg=risk_cfg, allow_outside_window_preview=True)
                st.rerun()

        auto_exec = bool(st.session_state.auto_execute) and risk_snapshot.can_trade
        st.caption(f"Last scan: {st.session_state.last_scan_time} • Feed: {st.session_state.data_source}")

    evaluations: list[StockEvaluation] = st.session_state.evaluations
    matching_evals = [e for e in evaluations if e.is_buy_signal]

    if auto_exec and risk_snapshot.can_trade and matching_evals:
        executed_any = False
        for sig in matching_evals:
            if risk_snapshot.trades_used >= risk_cfg.max_trades_per_day:
                break
            sig_key = (sig.symbol, sig.candle_time)
            if sig.symbol in summary["open_symbols"] or sig_key in st.session_state.executed_signal_candles:
                continue
            ok, exec_msg = execute_single_trade(
                signal_eval=sig,
                mode=mode,
                risk_snapshot=risk_snapshot,
                risk_cfg=risk_cfg,
                open_symbols=summary["open_symbols"],
                index_trend=index_trend,
                enforce_nifty_filter=nifty_filter_on,
            )
            st.session_state.last_notice = ("success" if ok else "error", exec_msg)
            executed_any = True
            if ok:
                st.session_state.executed_signal_candles.add(sig_key)
                risk_snapshot, summary = build_risk_snapshot(mode=mode, risk_cfg=risk_cfg)
        if executed_any:
            st.rerun()

    if matching_evals:
        sig_df = pd.DataFrame(
            [
                {
                    "Stock": e.symbol,
                    "Entry Price": e.price,
                    "Stop-Loss": e.stop_loss,
                    "Target": e.target,
                    "Qty": e.qty,
                }
                for e in matching_evals
            ]
        )
        st.dataframe(
            sig_df,
            hide_index=True,
            alt="NSE stocks currently matching all 3 intraday BUY conditions",
            column_config={
                "Stock": st.column_config.TextColumn("Stock"),
                "Entry Price": st.column_config.NumberColumn("Entry Price", format="₹%.2f"),
                "Stop-Loss": st.column_config.NumberColumn("Stop-Loss", format="₹%.2f"),
                "Target": st.column_config.NumberColumn("Target", format="₹%.2f"),
                "Qty": st.column_config.NumberColumn("Qty", format="%d"),
            },
        )

        with st.container(horizontal=True, vertical_alignment="bottom"):
            selected_sym = st.selectbox(
                "Signal to execute",
                options=[e.symbol for e in matching_evals],
                key="selected_signal_symbol",
            )
            chosen_eval = next(e for e in matching_evals if e.symbol == selected_sym)
            can_exec, block_reason = can_execute_trade(
                snapshot=risk_snapshot,
                symbol=chosen_eval.symbol,
                open_symbols=summary["open_symbols"],
                qty=chosen_eval.qty,
                sl_distance=chosen_eval.sl_distance,
                config=risk_cfg,
            )
            if st.button(
                "Execute Trade",
                icon=":material/bolt:",
                key="execute_trade_btn",
                disabled=not can_exec or (mode == "LIVE" and not broker.is_connected),
                help=block_reason,
            ):
                ok, exec_msg = execute_single_trade(
                    signal_eval=chosen_eval,
                    mode=mode,
                    risk_snapshot=risk_snapshot,
                    risk_cfg=risk_cfg,
                    open_symbols=summary["open_symbols"],
                    index_trend=index_trend,
                    enforce_nifty_filter=nifty_filter_on,
                )
                st.session_state.last_notice = ("success" if ok else "error", exec_msg)
                st.rerun()
    else:
        st.info(
            "No stocks currently match all 3 BUY conditions (Price > VWAP, 9 EMA > 21 EMA bullish crossover, RSI 55–68 & Vol > 1.5x Avg20).",
            icon=":material/filter_alt:",
        )
        st.button(
            "Execute Trade",
            icon=":material/bolt:",
            key="execute_trade_btn_disabled",
            disabled=True,
        )

    diag_exp = st.expander(f"Screener diagnostics ({len(evaluations)} stocks)", on_change="rerun")
    if diag_exp.open:
        with diag_exp:
            if evaluations:
                diag_df = pd.DataFrame(
                    [
                        {
                            "Stock": e.symbol,
                            "Price": e.price,
                            "VWAP": e.vwap,
                            "EMA 9": e.ema_9,
                            "EMA 21": e.ema_21,
                            "RSI(14)": e.rsi_14,
                            "Vol Ratio": e.volume_ratio,
                            "Price>VWAP": e.cond_price_above_vwap,
                            "EMA Cross": e.cond_ema_bullish_crossover,
                            "RSI&Vol": e.cond_rsi_and_volume,
                            "Signal": "BUY" if e.is_buy_signal else ("ERR" if e.error else "WAIT"),
                            "Candle": e.candle_time,
                            "Note": e.error or "",
                        }
                        for e in evaluations
                    ]
                )
                st.dataframe(diag_df, hide_index=True, alt="Indicator breakdown for scanned stocks")
            else:
                st.caption("Run a scan to inspect 5-minute VWAP, EMA, RSI, and Volume metrics.")

st.space("small")

# Bottom Section: Today's Trade Book & Panic Exit
with st.container(border=True):
    tb_left, tb_right = st.columns([3, 2], vertical_alignment="center")
    with tb_left:
        st.subheader(f"Today's trade book ({mode})", anchor=False)
    with tb_right:
        with st.container(horizontal=True, horizontal_alignment="right"):
            if st.button(
                "Refresh LTPs",
                icon=":material/sync:",
                key="refresh_ltps_btn",
                disabled=(summary["open_positions_count"] == 0),
            ):
                today_df = trade_book.get_today_trades(mode=mode, segment="EQ_INTRADAY")
                open_rows = today_df[today_df["status"] == "OPEN"]
                open_stocks = [
                    {"symbol": str(r["symbol"]), "token": str(r["token"]), "yahoo_symbol": f"{r['symbol']}.NS"}
                    for _, r in open_rows.iterrows()
                ]
                fresh_ltps = broker.fetch_batch_ltp(open_stocks, require_smartapi=(mode == "LIVE"))
                st.session_state.ltp_map.update(fresh_ltps)
                st.rerun()

            if st.button(
                "Square Off All (Panic Exit)",
                type="primary",
                icon=":material/emergency_home:",
                key="panic_exit_btn",
                disabled=(summary["open_positions_count"] == 0),
            ):
                closed_cnt, msgs = execute_panic_square_off(mode=mode, segment="EQ_INTRADAY")
                level = "success" if closed_cnt > 0 else "error"
                st.session_state.last_notice = (level, " | ".join(msgs))
                st.rerun()

    today_trades = trade_book.get_today_trades(mode=mode, segment="EQ_INTRADAY")
    if today_trades.empty:
        st.caption("No equity trades logged today yet.")
    else:
        display_rows = []
        for _, row in today_trades.iterrows():
            sym = str(row["symbol"])
            status_val = str(row["status"])
            entry_px = float(row["entry_price"])
            qty = int(row["qty"])
            ltp = st.session_state.ltp_map.get(sym)
            if status_val == "OPEN":
                current_px = ltp if (ltp is not None and ltp > 0) else entry_px
                gross = round((current_px - entry_px) * qty, 2)
                chg = calculate_nse_charges("EQ_INTRADAY", entry_px * qty, current_px * qty)["total"]
                net_val = round(gross - chg, 2)
            else:
                current_px = float(row["exit_price"]) if pd.notna(row["exit_price"]) else entry_px
                gross = float(row["gross_pnl"]) if pd.notna(row["gross_pnl"]) else 0.0
                chg = float(row["charges"]) if pd.notna(row["charges"]) else 0.0
                net_val = float(row["realized_pnl"]) if pd.notna(row["realized_pnl"]) else 0.0

            display_rows.append(
                {
                    "Trade ID": row["trade_id"],
                    "Time": row["timestamp"],
                    "Stock": sym,
                    "Qty": qty,
                    "Entry": entry_px,
                    "Stop-Loss": float(row["stop_loss"]),
                    "Target": float(row["target"]),
                    "LTP / Exit": current_px,
                    "Charges": chg,
                    "Net P&L": net_val,
                    "Status": status_val,
                    "Reason": row["exit_reason"] if pd.notna(row["exit_reason"]) else "",
                }
            )

        tb_df = pd.DataFrame(display_rows)
        st.dataframe(
            tb_df,
            hide_index=True,
            alt="Today's open and closed equity intraday trades",
            column_config={
                "Entry": st.column_config.NumberColumn("Entry", format="₹%.2f"),
                "Stop-Loss": st.column_config.NumberColumn("Stop-Loss", format="₹%.2f"),
                "Target": st.column_config.NumberColumn("Target", format="₹%.2f"),
                "LTP / Exit": st.column_config.NumberColumn("LTP / Exit", format="₹%.2f"),
                "Charges": st.column_config.NumberColumn("Charges", format="₹%.2f"),
                "Net P&L": st.column_config.NumberColumn("Net P&L", format="₹%.2f"),
            },
        )
