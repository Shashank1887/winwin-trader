"""Optional Futures & Options (F&O) Assistant page with strict 1-lot risk guardrails."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from broker import AngelOneBroker, TradeBookManager
from risk_manager import (
    FNO_DAILY_MAX_LOSS,
    FNO_DAILY_PROFIT_TARGET,
    FNO_MAX_LOTS_CAP,
    FnoRiskConfig,
    RiskConfig,
    SegmentType,
    calculate_nse_charges,
    evaluate_risk_state,
    get_window_label,
    now_ist,
    round_to_tick,
)
from strategy import NIFTY50_ALL_SYMBOLS, evaluate_index_trend
from trading_actions import execute_panic_square_off

base_cfg: RiskConfig = st.session_state.active_risk_cfg
broker: AngelOneBroker = st.session_state.broker
trade_book: TradeBookManager = st.session_state.trade_book
trailing_on: bool = bool(st.session_state.get("trailing_sl_enabled", True))

st.session_state.setdefault("fno_paper_mode", True)
st.session_state.setdefault("fno_risk_ack", False)
st.session_state.setdefault("fno_underlying", "NIFTY")
st.session_state.setdefault("fno_instrument", "OPTIDX")
st.session_state.setdefault("fno_lots", 1)
st.session_state.setdefault("fno_sl_points", 15.0)
st.session_state.setdefault("fno_spot_trend", None)
st.session_state.setdefault("fno_contracts", [])
st.session_state.setdefault("fno_notice", None)

head_left, head_right = st.columns([3, 2], vertical_alignment="center")
with head_left:
    st.title("Futures & Options (Optional)", anchor=False)
    st.caption(
        f"Risk-Managed Option Buying (CE/PE) & 1-Lot Futures • Window: {get_window_label(base_cfg.window_preset)}"
    )
with head_right:
    with st.container(border=True):
        fno_paper = st.toggle(
            "F&O PAPER MODE (Default ON)",
            key="fno_paper_mode",
        )
        fno_mode = "PAPER" if fno_paper else "LIVE"
        if fno_paper:
            st.badge("F&O PAPER MODE — Virtual P&L in trades_log.csv", icon=":material/science:", color="green")
        else:
            st.badge("F&O LIVE MODE — Real NFO orders via SmartAPI", icon=":material/warning:", color="red")

fno_segments: tuple[SegmentType, ...] = ("NFO_OPTIONS", "NFO_FUTURES")
fno_cfg = FnoRiskConfig(
    capital=base_cfg.capital,
    max_lots=int(st.session_state.fno_lots),
    sl_points=float(st.session_state.fno_sl_points),
    daily_profit_target=FNO_DAILY_PROFIT_TARGET,
    daily_max_loss=FNO_DAILY_MAX_LOSS,
    max_trades_per_day=base_cfg.max_trades_per_day,
    window_preset=base_cfg.window_preset,
)

# Sync open F&O trades with live NFO LTPs if connected
if broker.is_connected:
    today_fno = trade_book.get_today_trades(mode=fno_mode, segment=fno_segments)
    open_fno = today_fno[today_df_mask] if (not today_fno.empty and (today_df_mask := (today_fno["status"] == "OPEN")).any()) else pd.DataFrame()
    if not open_fno.empty:
        nfo_list = [{"tradingsymbol": str(r["tradingsymbol"]), "token": str(r["token"])} for _, r in open_fno.iterrows()]
        try:
            st.session_state.ltp_map.update(broker.fetch_nfo_ltp(nfo_list, require_smartapi=False))
        except Exception:
            pass

fno_events = trade_book.sync_open_trades_with_ltp(
    mode=fno_mode,
    ltp_map=st.session_state.ltp_map,
    enable_trailing_sl=trailing_on,
    broker=broker if fno_mode == "LIVE" else None,
    segment=fno_segments,
)
for ev in fno_events:
    st.toast(f"{ev['symbol']}: {ev['reason']}")

fno_summary = trade_book.compute_today_summary(mode=fno_mode, ltp_map=st.session_state.ltp_map, segment=fno_segments)
fno_risk_cfg_equiv = RiskConfig(
    capital=fno_cfg.capital,
    daily_profit_target=fno_cfg.daily_profit_target,
    daily_max_loss=fno_cfg.daily_max_loss,
    max_sl_per_trade=400.0,
    max_trades_per_day=fno_cfg.max_trades_per_day,
    window_preset=fno_cfg.window_preset,
)
# Evaluate F&O risk using F&O daily target (+₹2,500) and F&O max loss (-₹1,500)
fno_snap = evaluate_risk_state(
    realized_pnl=fno_summary["realized_pnl"],
    unrealized_pnl=fno_summary["unrealized_pnl"],
    trades_used=fno_summary["trades_used"],
    open_positions_count=fno_summary["open_positions_count"],
    config=RiskConfig(
        capital=fno_cfg.capital,
        daily_profit_target=1000.0,
        daily_max_loss=800.0,
        max_sl_per_trade=400.0,
        max_trades_per_day=fno_cfg.max_trades_per_day,
        window_preset=fno_cfg.window_preset,
    ),
)
# Re-apply F&O-specific daily target & max loss thresholds cleanly
if fno_summary["net_pnl"] <= -fno_cfg.daily_max_loss or fno_summary["realized_pnl"] <= -fno_cfg.daily_max_loss:
    fno_status = "KILL-SWITCH LOCKED"
    fno_can_trade = False
    if fno_summary["open_positions_count"] > 0 and broker.is_connected:
        execute_panic_square_off(mode=fno_mode, segment=fno_segments)
elif fno_summary["realized_pnl"] >= fno_cfg.daily_profit_target:
    fno_status = "GOAL LOCKED"
    fno_can_trade = False
else:
    fno_status = "ACTIVE"
    fno_can_trade = fno_snap.within_active_window and (
        fno_cfg.max_trades_per_day == 0 or fno_summary["trades_used"] < fno_cfg.max_trades_per_day
    )

if not broker.is_connected:
    st.info(
        "Connect Angel One SmartAPI in the sidebar to fetch live NFO Option/Future premiums (`exchange='NFO'`). Spot index trend still works right now.",
        icon=":material/link:",
    )

if st.session_state.fno_notice:
    lvl, txt = st.session_state.fno_notice
    (st.success if lvl == "success" else st.error if lvl == "error" else st.info)(txt)
    st.session_state.fno_notice = None

# 4 Metric Cards for F&O
m1, m2, m3, m4 = st.columns(4)
m1.metric(
    "F&O Net P&L",
    f"₹{fno_summary['net_pnl']:+,.2f}",
    delta=f"Charges ₹{fno_summary['total_charges']:,.2f}",
    delta_color="off",
    border=True,
    icon=":material/account_balance_wallet:",
)
m2.metric(
    "F&O Daily Target",
    f"₹{fno_summary['realized_pnl']:,.0f} / ₹{fno_cfg.daily_profit_target:,.0f}",
    delta=f"Kill-Switch -₹{fno_cfg.daily_max_loss:,.0f}",
    delta_color="off",
    border=True,
    icon=":material/flag:",
)
fno_trades_label = (
    f"{fno_summary['trades_used']} / {fno_cfg.max_trades_per_day}"
    if fno_cfg.max_trades_per_day > 0
    else f"{fno_summary['trades_used']}"
)
m3.metric(
    "F&O Trades Used",
    fno_trades_label,
    delta=f"{fno_summary['open_positions_count']} open"
    + ("" if fno_cfg.max_trades_per_day > 0 else " (Unlimited)"),
    delta_color="off",
    border=True,
    icon=":material/swap_horiz:",
)
m4.metric(
    "F&O Status",
    fno_status,
    delta=get_window_label(base_cfg.window_preset),
    delta_color="off",
    border=True,
    icon=":material/shield:",
)

st.space("small")

with st.container(border=True):
    st.subheader("F&O Spot Signal & Contract Selector", anchor=False)
    st.caption("Manual Index & Stock Option Buying (CE/PE) and 1-Lot Futures only — naked Option Selling is blocked for capital safety.")

    index_symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY"]
    all_underlyings = index_symbols + [s for s in NIFTY50_ALL_SYMBOLS if s not in index_symbols]

    fc1, fc2, fc3, fc4 = st.columns(4)
    with fc1:
        underlying = st.selectbox("Index / Stock", all_underlyings, key="fno_underlying")
    is_index = underlying in index_symbols
    seg_options = ["OPTIDX", "FUTIDX"] if is_index else ["OPTSTK", "FUTSTK"]
    if st.session_state.get("fno_instrument") not in seg_options:
        st.session_state.fno_instrument = seg_options[0]

    with fc2:
        instrument = st.selectbox(
            "Segment",
            seg_options,
            format_func=lambda x: {
                "OPTIDX": "Index Options (CE / PE Buy)",
                "FUTIDX": "Index Futures (Buy)",
                "OPTSTK": "Stock Options (CE / PE Buy)",
                "FUTSTK": "Stock Futures (Buy)",
            }[x],
            key="fno_instrument",
        )
    with fc3:
        lots = st.number_input("Lots (max 2)", min_value=1, max_value=FNO_MAX_LOTS_CAP, step=1, key="fno_lots")
    with fc4:
        sl_pts = st.number_input("Stop-Loss (points)", min_value=5.0, max_value=50.0, step=2.5, key="fno_sl_points")

    opt_choice = "AUTO"
    if instrument.startswith("OPT"):
        opt_choice = st.segmented_control(
            "Option type",
            options=["AUTO", "CE", "PE"],
            default="AUTO",
            format_func=lambda x: {
                "AUTO": "Auto (Spot Trend)",
                "CE": "CE (Call Buy)",
                "PE": "PE (Put Buy)",
            }[x],
            key="fno_option_side",
        ) or "AUTO"

    if st.button("Analyze Spot & Load Nearest Contracts", icon=":material/radar:", key="fno_scan_btn"):
        try:
            spot_df, _ = broker.fetch_fno_underlying_candles(underlying, use_smartapi=(fno_mode == "LIVE"), days_back=5)
            trend = evaluate_index_trend(spot_df, index_name=underlying)
            st.session_state.fno_spot_trend = trend
            if opt_choice in ("CE", "PE"):
                opt_side = opt_choice
            else:
                opt_side = "PE" if trend.regime == "BEARISH" else "CE"
            contracts = broker.get_fno_contracts(
                underlying=underlying,
                instrument_type=instrument,
                spot_price=trend.price if trend.price > 0 else None,
                option_type=opt_side,
                max_strikes=5,
            )
            if broker.is_connected and contracts:
                ltps = broker.fetch_nfo_ltp(contracts, require_smartapi=False)
                st.session_state.ltp_map.update(ltps)
            st.session_state.fno_contracts = contracts
        except Exception as exc:
            st.error(f"F&O analysis failed: {exc}")

    trend = st.session_state.fno_spot_trend
    if trend is not None:
        color = "green" if trend.regime == "BULLISH" else ("red" if trend.regime == "BEARISH" else "gray")
        st.badge(
            f"{trend.index_name} Spot: {trend.price:,.2f} | {trend.regime} | EMA9 {trend.ema_9:,.1f} / EMA21 {trend.ema_21:,.1f} | RSI {trend.rsi_14} -> {trend.recommended_fno_action}",
            color=color,
        )

    contracts = st.session_state.fno_contracts
    if contracts:
        selected_tsym = st.selectbox(
            "Select NFO Contract",
            options=[c["tradingsymbol"] for c in contracts],
            key="fno_selected_contract",
        )
        chosen = next(c for c in contracts if c["tradingsymbol"] == selected_tsym)
        qty = int(chosen["lotsize"]) * int(lots)
        live_ltp = st.session_state.ltp_map.get(selected_tsym, 0.0)

        col_p, col_r = st.columns(2)
        with col_p:
            entry_px = st.number_input(
                "Entry Premium / Price (₹)",
                min_value=1.0,
                value=float(live_ltp) if live_ltp > 0 else 100.0,
                step=0.5,
                help="Auto-filled from SmartAPI NFO LTP when connected.",
                key="fno_entry_px_input",
            )
        sl_px = round_to_tick(max(0.05, entry_px - float(sl_pts)))
        tgt_px = round_to_tick(entry_px + 2.0 * float(sl_pts))
        seg_type: SegmentType = "NFO_OPTIONS" if instrument.startswith("OPT") else "NFO_FUTURES"
        est_chg = calculate_nse_charges(seg_type, entry_px * qty, tgt_px * qty)["total"]
        with col_r:
            st.metric(
                "1:2 Trade Plan",
                f"Qty {qty} ({lots} lot)",
                delta=f"SL ₹{sl_px:.2f} | Tgt ₹{tgt_px:.2f} | Max Risk ₹{qty * float(sl_pts):,.0f}",
                delta_color="off",
                border=True,
            )
            st.caption(f"Expiry: {chosen['expiry']} • Est. NFO Charges: ₹{est_chg:,.2f}")

        ack = st.checkbox(
            "Enable F&O Execution (I accept 1-lot F&O risk and SL rules)",
            key="fno_risk_ack",
        )
        exec_disabled = (not ack) or (not fno_can_trade) or (fno_mode == "LIVE" and not broker.is_connected)
        if st.button(
            f"Execute F&O BUY ({selected_tsym})",
            icon=":material/bolt:",
            key="fno_exec_btn",
            disabled=exec_disabled,
        ):
            if fno_mode == "PAPER":
                fill_px = live_ltp if live_ltp > 0 else entry_px
                trade_book.record_entry(
                    mode="PAPER",
                    symbol=selected_tsym,
                    tradingsymbol=selected_tsym,
                    token=str(chosen["token"]),
                    qty=qty,
                    entry_price=fill_px,
                    stop_loss=round_to_tick(max(0.05, fill_px - float(sl_pts))),
                    target=round_to_tick(fill_px + 2.0 * float(sl_pts)),
                    notes=f"F&O Paper ({chosen['expiry']})",
                    segment=seg_type,
                    exchange="NFO",
                )
                st.session_state.fno_notice = ("success", f"F&O PAPER BUY logged: {qty} x {selected_tsym} @ ₹{fill_px:.2f}")
                st.rerun()
            else:
                try:
                    res = broker.place_live_buy_with_protection(
                        symbol=selected_tsym,
                        tradingsymbol=selected_tsym,
                        token=str(chosen["token"]),
                        qty=qty,
                        entry_price_hint=entry_px,
                        stop_loss=sl_px,
                        target=tgt_px,
                        exchange="NFO",
                    )
                    trade_book.record_entry(
                        mode="LIVE",
                        symbol=selected_tsym,
                        tradingsymbol=selected_tsym,
                        token=str(chosen["token"]),
                        qty=qty,
                        entry_price=res["fill_price"],
                        stop_loss=sl_px,
                        target=tgt_px,
                        entry_order_id=res["entry_order_id"],
                        sl_order_id=res["sl_order_id"],
                        target_order_id=res["target_order_id"],
                        notes=res["notes"],
                        segment=seg_type,
                        exchange="NFO",
                    )
                    st.session_state.fno_notice = ("success", f"F&O LIVE BUY placed: {selected_tsym} (ID: {res['entry_order_id']})")
                    st.rerun()
                except Exception as exc:
                    st.session_state.fno_notice = ("error", str(exc))
                    st.rerun()

st.space("small")

with st.container(border=True):
    ft_left, ft_right = st.columns([3, 2], vertical_alignment="center")
    with ft_left:
        st.subheader(f"Today's F&O trade book ({fno_mode})", anchor=False)
    with ft_right:
        with st.container(horizontal=True, horizontal_alignment="right"):
            if st.button(
                "Square Off All F&O (Panic Exit)",
                type="primary",
                icon=":material/emergency_home:",
                key="fno_panic_btn",
                disabled=(fno_summary["open_positions_count"] == 0),
            ):
                cnt, msgs = execute_panic_square_off(mode=fno_mode, segment=fno_segments)
                st.session_state.fno_notice = ("success" if cnt > 0 else "error", " | ".join(msgs))
                st.rerun()

    today_fno_df = trade_book.get_today_trades(mode=fno_mode, segment=fno_segments)
    if today_fno_df.empty:
        st.caption("No F&O trades logged today yet.")
    else:
        st.dataframe(today_fno_df[["trade_id", "timestamp", "segment", "tradingsymbol", "qty", "entry_price", "stop_loss", "target", "status", "charges", "realized_pnl", "exit_reason"]], hide_index=True, alt="Today's F&O trades")
