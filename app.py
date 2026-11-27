"""Single entrypoint for the Intraday Trading Assistant (Equity Screener + Optional F&O)."""

from __future__ import annotations

import streamlit as st

from broker import (
    AngelCredentials,
    AngelOneBroker,
    BrokerAuthError,
    TradeBookManager,
    load_credentials,
    save_credentials,
)
from notifier import (
    TelegramConfig,
    load_telegram_config,
    save_telegram_config,
    send_telegram_message,
)
from risk_manager import (
    DAILY_MAX_LOSS,
    DAILY_PROFIT_TARGET,
    MAX_SL_PER_TRADE,
    MAX_TRADES_PER_DAY,
    RiskConfig,
    WindowPreset,
    get_window_label,
)
from strategy import StockEvaluation, UniversePreset

st.set_page_config(
    page_title="Intraday Trading Assistant & Screener",
    page_icon=":material/candlestick_chart:",
    layout="wide",
)


def init_session_state() -> None:
    """Initialize persistent per-session objects once across pages."""
    if "creds" not in st.session_state:
        st.session_state.creds = load_credentials()
    if "tg_config" not in st.session_state or not isinstance(st.session_state.tg_config, TelegramConfig):
        st.session_state.tg_config = load_telegram_config()
    if "notified_signal_candles" not in st.session_state:
        st.session_state.notified_signal_candles: set[tuple[str, str]] = set()
    if "broker" not in st.session_state or not isinstance(st.session_state.broker, AngelOneBroker):
        old_broker = st.session_state.get("broker")
        new_broker = AngelOneBroker(st.session_state.creds)
        if old_broker is not None and getattr(old_broker, "is_connected", False):
            new_broker.client = getattr(old_broker, "client", None)
            new_broker.is_connected = True
            new_broker.profile_name = getattr(old_broker, "profile_name", "")
        st.session_state.broker = new_broker
    if "trade_book" not in st.session_state or not isinstance(st.session_state.trade_book, TradeBookManager):
        st.session_state.trade_book = TradeBookManager("trades_log.csv")
    if "evaluations" not in st.session_state:
        st.session_state.evaluations: list[StockEvaluation] = []
    if "ltp_map" not in st.session_state:
        st.session_state.ltp_map: dict[str, float] = {}
    if "last_scan_time" not in st.session_state:
        st.session_state.last_scan_time = "Not scanned yet"
    if "data_source" not in st.session_state:
        st.session_state.data_source = "-"
    if "last_notice" not in st.session_state:
        st.session_state.last_notice: tuple[str, str] | None = None
    if "executed_signal_candles" not in st.session_state:
        st.session_state.executed_signal_candles: set[tuple[str, str]] = set()
    st.session_state.setdefault("index_trend", None)
    st.session_state.setdefault("paper_mode", True)
    st.session_state.setdefault("auto_execute", False)
    st.session_state.setdefault("autopilot_enabled", False)
    st.session_state.setdefault("autopilot_interval", "60s")
    st.session_state.setdefault("autopilot_auto_eod", False)
    st.session_state.setdefault("autopilot_log", [])
    st.session_state.setdefault("autopilot_last_scan_ep", 0.0)
    st.session_state.setdefault("capital", 50000.0)
    st.session_state.setdefault("max_sl_per_trade", MAX_SL_PER_TRADE)
    st.session_state.setdefault("enable_max_trades_limit", False)
    st.session_state.setdefault("max_trades_per_day", 5)
    st.session_state.setdefault("window_preset", "morning")
    st.session_state.setdefault("universe_preset", "nifty50")
    st.session_state.setdefault("last_scan_candles", {})
    st.session_state.setdefault("backtest_result", None)
    st.session_state.setdefault("custom_symbols", "RELIANCE, TCS, TRENT, BEL, HAL")
    st.session_state.setdefault("trailing_sl_enabled", True)
    st.session_state.setdefault("nifty_trend_filter", True)
    st.session_state.setdefault("enable_fno_page", True)


def render_sidebar() -> RiskConfig:
    """Render shared Angel One credentials (.env) and Capital & Risk sidebar."""
    broker: AngelOneBroker = st.session_state.broker
    creds: AngelCredentials = st.session_state.creds

    with st.sidebar:
        st.header("Angel One SmartAPI", divider="gray")
        if broker.is_connected:
            st.badge(f"Connected: {broker.profile_name}", icon=":material/verified_user:", color="green")
        else:
            st.badge("Disconnected", icon=":material/link_off:", color="gray")

        with st.form("angel_credentials_form"):
            api_key = st.text_input("API Key", value=creds.api_key, type="password", key="sb_api_key")
            client_id = st.text_input("Client ID", value=creds.client_id, key="sb_client_id")
            password = st.text_input("Password / PIN", value=creds.password, type="password", key="sb_password")
            totp_secret = st.text_input("TOTP Secret", value=creds.totp_secret, type="password", key="sb_totp_secret")
            submitted = st.form_submit_button("Save to .env & Connect", icon=":material/login:", width="stretch")

        if submitted:
            new_creds = AngelCredentials(
                api_key=api_key,
                client_id=client_id,
                password=password,
                totp_secret=totp_secret,
            )
            save_credentials(new_creds)
            st.session_state.creds = new_creds
            try:
                broker.connect(new_creds)
                st.success(f"Connected as {broker.profile_name}. Credentials saved to .env.")
            except BrokerAuthError as exc:
                st.error(str(exc))

        if broker.is_connected:
            if st.button("Disconnect session", icon=":material/logout:", width="stretch", key="sb_disconnect"):
                broker.disconnect()
                st.rerun()

        st.header("Capital & Risk Settings", divider="gray")
        capital = st.number_input(
            "Trading capital (₹)",
            min_value=5000.0,
            max_value=5000000.0,
            step=5000.0,
            key="capital",
        )
        max_sl = st.number_input(
            "Max SL per trade (₹)",
            min_value=50.0,
            max_value=MAX_SL_PER_TRADE,
            step=50.0,
            key="max_sl_per_trade",
            help=f"Hardcoded ceiling: ₹{MAX_SL_PER_TRADE:,.0f} per trade",
        )
        limit_trades = st.toggle(
            "Limit max trades per day (optional)",
            key="enable_max_trades_limit",
            help="Off = unlimited trades (still protected by Daily Target +₹1,000 & Kill-Switch -₹800).",
        )
        if limit_trades:
            max_trades = int(
                st.number_input(
                    "Max trades per day",
                    min_value=1,
                    max_value=50,
                    step=1,
                    key="max_trades_per_day",
                )
            )
        else:
            max_trades = 0
        window_preset: WindowPreset = st.selectbox(
            "Active trading window",
            options=["morning", "split", "full"],
            format_func=get_window_label,
            key="window_preset",
        )
        universe_preset: UniversePreset = st.selectbox(
            "Stock screener universe",
            options=["nifty50", "top15", "custom"],
            format_func=lambda k: {
                "nifty50": "Full Nifty 50 — All 50 Stocks (0.35s batch)",
                "top15": "Top 15 Liquid Nifty 50 (~6s)",
                "custom": "Custom NSE Watchlist",
            }[k],
            key="universe_preset",
        )
        if universe_preset == "custom":
            st.text_input(
                "Custom NSE symbols",
                key="custom_symbols",
                help="Comma-separated NSE equity symbols, e.g. TRENT, BEL, HAL, TITAN",
            )

        st.subheader("Safety & Accuracy Filters", divider="gray")
        st.toggle(
            "Trailing SL + Break-Even (+1R/+1.5R)",
            key="trailing_sl_enabled",
            help="Moves SL to Cost at +1R and locks +0.75R profit at +1.5R.",
        )
        st.toggle(
            "NIFTY 50 Index Trend Filter",
            key="nifty_trend_filter",
            help="Blocks counter-trend Equity trades (BUY when NIFTY BEARISH, SELL when BULLISH).",
        )
        st.toggle(
            "Show F&O Trading Page (Optional)",
            key="enable_fno_page",
        )

        st.caption("Hardcoded Guardrails")
        st.table(
            {
                "Daily Target": f"+₹{DAILY_PROFIT_TARGET:,.0f} (Net Auto-Lock)",
                "Daily Kill-Switch": f"-₹{DAILY_MAX_LOSS:,.0f} (Hard Stop)",
                "Max SL / Trade": f"₹{MAX_SL_PER_TRADE:,.0f}",
                "Max Trades / Day": f"{max_trades}" if max_trades > 0 else "Unlimited (Optional)",
                "Selected Window": get_window_label(window_preset),
            },
            border="horizontal",
        )

        tg_exp = st.expander("Telegram Mobile Alerts", on_change="rerun")
        if tg_exp.open:
            with tg_exp:
                tg_cfg: TelegramConfig = st.session_state.tg_config
                tg_token = st.text_input("Bot Token", value=tg_cfg.bot_token, type="password", key="sb_tg_token")
                tg_chat = st.text_input("Chat ID", value=tg_cfg.chat_id, key="sb_tg_chat")
                tg_on = st.toggle("Enable Telegram alerts", value=tg_cfg.enabled, key="sb_tg_enabled")
                c_tg1, c_tg2 = st.columns(2)
                if c_tg1.button("Save Alerts", key="sb_tg_save", width="stretch"):
                    new_tg = TelegramConfig(bot_token=tg_token, chat_id=tg_chat, enabled=tg_on)
                    save_telegram_config(new_tg)
                    st.session_state.tg_config = new_tg
                    st.success("Saved to .env")
                if c_tg2.button("Test Alert", key="sb_tg_test", width="stretch"):
                    test_tg = TelegramConfig(bot_token=tg_token, chat_id=tg_chat, enabled=True)
                    ok, detail = send_telegram_message("WinWin Trader: Test alert connected!", cfg=test_tg)
                    (st.success if ok else st.error)(detail)

        mobile_exp = st.expander("Mobile Quick Connect (1-Tap Link)", on_change="rerun")
        if mobile_exp.open:
            with mobile_exp:
                import os
                import socket
                from pathlib import Path
                from dotenv import dotenv_values

                token = os.getenv("MOBILE_API_TOKEN", "").strip()
                if not token and Path(".env").exists():
                    token = str(dotenv_values(".env").get("MOBILE_API_TOKEN") or "").strip()
                lan_ip = "127.0.0.1"
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                        s.connect(("8.8.8.8", 80))
                        lan_ip = s.getsockname()[0]
                except Exception:
                    pass
                if token:
                    st.caption("Open this link once on iPhone / OnePlus Pad Go (auto-saves token):")
                    st.code(f"http://{lan_ip}:8000/#token={token}", language=None)
                else:
                    st.caption("Start api_server.py once to generate MOBILE_API_TOKEN in .env.")

    return RiskConfig(
        capital=capital,
        daily_profit_target=DAILY_PROFIT_TARGET,
        daily_max_loss=DAILY_MAX_LOSS,
        max_sl_per_trade=max_sl,
        max_trades_per_day=int(max_trades),
        window_preset=window_preset,
    )


init_session_state()
st.session_state.active_risk_cfg = render_sidebar()

pages = [
    st.Page(
        "app_pages/equity_screener.py",
        title="Equity Screener",
        icon=":material/candlestick_chart:",
        default=True,
    )
]
if st.session_state.get("enable_fno_page", True):
    pages.append(
        st.Page(
            "app_pages/fno_trading.py",
            title="F&O Trading (Optional)",
            icon=":material/query_stats:",
        )
    )

nav = st.navigation(pages, position="top")
nav.run()
