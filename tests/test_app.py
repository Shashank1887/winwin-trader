"""Unit and Streamlit AppTest verification for Intraday Trading Assistant."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from broker import (
    AngelCredentials,
    AngelOneBroker,
    RateLimitError,
    TradeBookManager,
    load_credentials,
    save_credentials,
)
from risk_manager import (
    DAILY_MAX_LOSS,
    DAILY_PROFIT_TARGET,
    IST,
    MAX_SL_PER_TRADE,
    MAX_TRADES_PER_DAY,
    RiskConfig,
    calculate_nse_charges,
    calculate_position_sizing,
    can_execute_trade,
    compute_trailing_stop_loss,
    evaluate_risk_state,
    is_within_active_window,
)
from strategy import (
    NIFTY50_LIQUID_UNIVERSE,
    StockEvaluation,
    compute_intraday_vwap,
    evaluate_index_trend,
    evaluate_stock_setup,
    run_strategy_backtest,
)


def _make_candles_for_crossover(
    rsi_target_mode: str = "sweet_spot",
    volume_spike: bool = True,
    above_vwap: bool = True,
) -> pd.DataFrame:
    """Construct deterministic OHLCV candles with closed signal at iloc[-2] and trailing live bar iloc[-1]."""
    start = datetime(2026, 11, 25, 9, 15, tzinfo=IST)
    closes = [
        100.8, 100.6, 100.7, 100.5, 100.6, 100.4, 100.5, 100.3, 100.2, 100.3,
        100.1, 100.0, 100.1, 99.9, 99.8, 99.9, 99.7, 99.6, 99.7, 99.5,
        99.4, 99.5, 99.6, 99.7, 99.8, 99.9, 100.0, 100.1, 100.2, 100.9,
    ]
    if rsi_target_mode == "overbought":
        closes = [100.0 + i * 1.4 for i in range(30)]
    elif rsi_target_mode == "weak":
        closes = [120.0 - i * 0.8 for i in range(29)] + [98.0]
    elif rsi_target_mode == "sell_sweet_spot":
        # Mild downward drift keeping RSI(14) cleanly inside 30..48 with 9 EMA < 21 EMA
        closes = [110.0]
        for i in range(1, 30):
            step = -0.22 if i % 2 == 1 else 0.17
            closes.append(round(closes[-1] + step, 2))

    # Append an incomplete live bar (iloc[-1]) so closed-candle signal sits cleanly at iloc[-2]
    all_closes = closes + [closes[-1] - 0.1]

    rows = []
    closed_idx = len(all_closes) - 2
    for idx, close in enumerate(all_closes):
        ts = start + timedelta(minutes=5 * idx)
        vol = 10000
        if idx == closed_idx:
            vol = 20000 if volume_spike else 8000
        elif idx == len(all_closes) - 1:
            vol = 5000  # Incomplete live bar volume should be ignored
        open_px = close - 0.3
        high_px = close + 0.4
        low_px = close - 0.5
        rows.append(
            {
                "timestamp": ts,
                "open": open_px,
                "high": high_px,
                "low": low_px,
                "close": close,
                "volume": vol,
            }
        )

    df = pd.DataFrame(rows)
    if rsi_target_mode == "sell_sweet_spot":
        # Push early session prices higher or equal so Close < VWAP cleanly at iloc[-2]
        df.loc[0:3, "high"] = 115.0
        df.loc[0:3, "low"] = 113.0
        df.loc[0:3, "close"] = 114.0
        df.loc[0:3, "volume"] = 50000
    elif not above_vwap:
        # Inflate early intraday candle volume & price so session VWAP stays above closed iloc[-2]
        df.loc[0:5, "high"] = 130.0
        df.loc[0:5, "low"] = 128.0
        df.loc[0:5, "close"] = 129.0
        df.loc[0:5, "volume"] = 200000
    return df


def test_risk_config_hardcoded_ceilings() -> None:
    cfg = RiskConfig(
        capital=100000,
        daily_profit_target=5000,
        daily_max_loss=5000,
        max_sl_per_trade=2000,
        max_trades_per_day=10,
    )
    assert cfg.daily_profit_target == DAILY_PROFIT_TARGET
    assert cfg.daily_max_loss == DAILY_MAX_LOSS
    assert cfg.max_sl_per_trade == MAX_SL_PER_TRADE
    assert cfg.max_trades_per_day == 10
    assert RiskConfig().max_trades_per_day == MAX_TRADES_PER_DAY == 0


def test_active_window_ist_boundaries() -> None:
    assert not is_within_active_window(datetime(2026, 11, 25, 9, 19, tzinfo=IST))
    assert is_within_active_window(datetime(2026, 11, 25, 9, 20, tzinfo=IST))
    assert is_within_active_window(datetime(2026, 11, 25, 11, 30, tzinfo=IST))
    assert not is_within_active_window(datetime(2026, 11, 25, 11, 31, tzinfo=IST))

    # Split window allows morning + afternoon (13:45-14:55), blocks midday chop
    assert not is_within_active_window(datetime(2026, 11, 25, 12, 30, tzinfo=IST), window_preset="split")
    assert is_within_active_window(datetime(2026, 11, 25, 14, 15, tzinfo=IST), window_preset="split")
    assert not is_within_active_window(datetime(2026, 11, 25, 15, 0, tzinfo=IST), window_preset="split")

    # Full window runs 09:20-15:00 IST
    assert is_within_active_window(datetime(2026, 11, 25, 14, 55, tzinfo=IST), window_preset="full")
    assert not is_within_active_window(datetime(2026, 11, 25, 15, 1, tzinfo=IST), window_preset="full")


def test_universe_presets_nifty50_and_custom() -> None:
    broker = AngelOneBroker()
    top15 = broker.resolve_universe_tokens("top15")
    full50 = broker.resolve_universe_tokens("nifty50")
    custom = broker.resolve_universe_tokens("custom", "TRENT, BEL.NS, HAL-EQ, TRENT")
    assert len(top15) == 15
    assert len(full50) == 50
    assert [s["symbol"] for s in custom] == ["TRENT", "BEL", "HAL"]


def test_position_sizing_fixed_400_sl_and_2x_target() -> None:
    sizing = calculate_position_sizing(entry_price=500.0, stop_loss_price=496.0, capital=100000.0)
    assert sizing.sl_distance == 4.0
    assert sizing.qty == 100
    assert sizing.max_loss_rupees == 400.0
    assert sizing.target == 508.0
    assert sizing.target_profit_rupees == 800.0


def test_risk_locks_goal_and_kill_switch() -> None:
    in_window = datetime(2026, 11, 25, 10, 0, tzinfo=IST)

    goal_snap = evaluate_risk_state(1000.0, 0.0, 1, 0, current_dt=in_window)
    assert goal_snap.system_status == "GOAL LOCKED"
    assert not goal_snap.can_trade

    kill_snap = evaluate_risk_state(-500.0, -300.0, 2, 1, current_dt=in_window)
    assert kill_snap.system_status == "KILL-SWITCH LOCKED"
    assert not kill_snap.can_trade
    assert kill_snap.should_auto_square_off

    capped_cfg = RiskConfig(max_trades_per_day=2)
    cap_snap = evaluate_risk_state(200.0, 0.0, 2, 0, config=capped_cfg, current_dt=in_window)
    assert cap_snap.system_status == "ACTIVE"
    assert not cap_snap.can_trade

    # Optional max_trades_per_day=0 allows more trades unless goal/kill-switch hits
    unlimited_snap = evaluate_risk_state(200.0, 0.0, 5, 0, config=RiskConfig(max_trades_per_day=0), current_dt=in_window)
    assert unlimited_snap.can_trade
    ok_unlimited, _ = can_execute_trade(unlimited_snap, "SBIN", set(), qty=50, sl_distance=8.0, config=RiskConfig(max_trades_per_day=0))
    assert ok_unlimited

    ok_snap = evaluate_risk_state(200.0, 0.0, 1, 0, config=capped_cfg, current_dt=in_window)
    eligible, _ = can_execute_trade(ok_snap, "SBIN", set(), qty=50, sl_distance=8.0, config=capped_cfg)
    assert eligible
    blocked_dup, _ = can_execute_trade(ok_snap, "SBIN", {"SBIN"}, qty=50, sl_distance=8.0, config=capped_cfg)
    assert not blocked_dup


def test_strategy_universe_and_vwap_session_reset() -> None:
    assert len(NIFTY50_LIQUID_UNIVERSE) == 15
    df = pd.DataFrame(
        {
            "timestamp": [
                datetime(2026, 11, 24, 9, 15, tzinfo=IST),
                datetime(2026, 11, 25, 9, 15, tzinfo=IST),
            ],
            "high": [200.0, 102.0],
            "low": [198.0, 98.0],
            "close": [199.0, 100.0],
            "volume": [100000, 1000],
        }
    )
    vwap = compute_intraday_vwap(df)
    assert pytest.approx(float(vwap.iloc[1]), rel=1e-4) == 100.0


def test_strategy_buy_and_sell_closed_candle_rules() -> None:
    # BUY setup evaluated on iloc[-2] (live incomplete iloc[-1] ignored)
    df_valid = _make_candles_for_crossover(rsi_target_mode="sweet_spot", volume_spike=True, above_vwap=True)
    # Tamper with live iloc[-1] bar drastically downward — should NOT negate BUY from completed iloc[-2]
    df_valid.loc[df_valid.index[-1], "close"] = 85.0
    df_valid.loc[df_valid.index[-1], "volume"] = 1000.0
    ev_valid = evaluate_stock_setup("SBIN", "SBIN-EQ", "3045", df_valid, capital=100000.0)
    assert ev_valid.cond_price_above_vwap
    assert ev_valid.cond_ema_bullish_crossover
    assert 52.0 <= ev_valid.rsi_14 <= 70.0
    assert ev_valid.volume_ratio > 1.2
    assert ev_valid.is_buy_signal
    assert ev_valid.side == "BUY"
    assert ev_valid.qty > 0
    assert ev_valid.qty * ev_valid.sl_distance <= MAX_SL_PER_TRADE

    # Fake breakout ONLY on incomplete iloc[-1] must NOT trigger a signal
    df_no_vol = _make_candles_for_crossover(rsi_target_mode="sweet_spot", volume_spike=False, above_vwap=True)
    df_no_vol.loc[df_no_vol.index[-1], "volume"] = 500000.0
    df_no_vol.loc[df_no_vol.index[-1], "close"] = 115.0
    ev_no_vol = evaluate_stock_setup("SBIN", "SBIN-EQ", "3045", df_no_vol, capital=100000.0)
    assert not ev_no_vol.is_buy_signal
    assert not ev_no_vol.is_sell_signal
    assert ev_no_vol.side == "WAIT"

    df_below_vwap = _make_candles_for_crossover(rsi_target_mode="sweet_spot", volume_spike=True, above_vwap=False)
    ev_below_vwap = evaluate_stock_setup("SBIN", "SBIN-EQ", "3045", df_below_vwap, capital=100000.0)
    assert not ev_below_vwap.cond_price_above_vwap
    assert not ev_below_vwap.is_buy_signal

    # SELL (Short) setup on iloc[-2]: Close < VWAP, 9 EMA < 21 EMA, 30 <= RSI <= 48, Vol > 1.2x
    df_sell = _make_candles_for_crossover(rsi_target_mode="sell_sweet_spot", volume_spike=True)
    ev_sell = evaluate_stock_setup("SBIN", "SBIN-EQ", "3045", df_sell, capital=100000.0)
    assert ev_sell.cond_price_below_vwap
    assert ev_sell.cond_ema_bearish
    assert 30.0 <= ev_sell.rsi_14 <= 48.0
    assert ev_sell.cond_sell_rsi_and_volume
    assert ev_sell.is_sell_signal
    assert ev_sell.side == "SELL"
    assert ev_sell.stop_loss > ev_sell.price
    assert ev_sell.target < ev_sell.price
    assert ev_sell.qty * ev_sell.sl_distance <= MAX_SL_PER_TRADE


def test_sell_sizing_trailing_sl_and_backtest_5_days(tmp_path: Path) -> None:
    sell_sizing = calculate_position_sizing(entry_price=500.0, stop_loss_price=504.0, capital=100000.0, side="SELL")
    assert sell_sizing.sl_distance == 4.0
    assert sell_sizing.qty == 100
    assert sell_sizing.target == 492.0

    # Trailing SL for SELL trails DOWN
    sl0, _ = compute_trailing_stop_loss(500.0, 504.0, 492.0, 498.0, side="SELL")
    assert sl0 == 504.0
    sl_be, st_be = compute_trailing_stop_loss(500.0, 504.0, 492.0, 496.0, side="SELL")
    assert sl_be == 500.0 and "BREAK-EVEN" in st_be
    sl_lk, st_lk = compute_trailing_stop_loss(500.0, 500.0, 492.0, 494.0, side="SELL")
    assert sl_lk == 497.0 and "PROFIT LOCK" in st_lk

    # Short trade close & P&L check in TradeBookManager
    tb = TradeBookManager(tmp_path / "trades_short.csv")
    tb.record_entry("PAPER", "SBIN", "SBIN-EQ", "3045", qty=100, entry_price=500.0, stop_loss=504.0, target=492.0, side="SELL")
    evs = tb.sync_open_trades_with_ltp("PAPER", {"SBIN": 491.5})
    assert len(evs) == 1 and evs[0]["reason"] == "TARGET HIT"
    assert 700.0 < evs[0]["pnl"] < 800.0

    # 5-Day Backtest across BUY & SELL data
    df_buy = _make_candles_for_crossover(rsi_target_mode="sweet_spot", volume_spike=True, above_vwap=True)
    # Add follow-through target bar on the same session for backtest simulation
    extra_bar = pd.DataFrame(
        [
            {
                "timestamp": datetime(2026, 11, 25, 11, 50, tzinfo=IST),
                "open": 100.9,
                "high": 106.0,
                "low": 100.8,
                "close": 105.5,
                "volume": 12000,
            }
        ]
    )
    bt = run_strategy_backtest({"SBIN": pd.concat([df_buy, extra_bar], ignore_index=True)}, days=5)
    assert bt["stocks_tested"] == 1
    assert bt["total_signals"] >= 1
    assert len(bt["summary_rows"]) == 3


def test_trailing_sl_and_nse_charges() -> None:
    # Entry 100, Initial SL 96 (1R = 4), Target 108
    sl_unchanged, stage0 = compute_trailing_stop_loss(100.0, 96.0, 108.0, 102.0)
    assert sl_unchanged == 96.0 and stage0 == ""

    sl_be, stage1 = compute_trailing_stop_loss(100.0, 96.0, 108.0, 104.0)
    assert sl_be == 100.0 and "BREAK-EVEN" in stage1

    sl_lock, stage2 = compute_trailing_stop_loss(100.0, 100.0, 108.0, 106.0)
    assert sl_lock == 103.0 and "PROFIT LOCK" in stage2

    eq_chg = calculate_nse_charges("EQ_INTRADAY", 50000.0, 50400.0)
    opt_chg = calculate_nse_charges("NFO_OPTIONS", 6500.0, 8450.0)
    assert eq_chg["total"] > 0 and eq_chg["brokerage"] <= 40.0
    assert opt_chg["brokerage"] == 40.0 and opt_chg["total"] > 40.0


def test_index_trend_evaluation() -> None:
    start = datetime(2026, 11, 25, 9, 15, tzinfo=IST)
    bull_df = pd.DataFrame(
        [
            {
                "timestamp": start + timedelta(minutes=5 * i),
                "open": 24000 + i * 5.0,
                "high": 24008 + i * 5.0,
                "low": 23996 + i * 5.0,
                "close": 24005 + i * 5.0,
            }
            for i in range(30)
        ]
    )
    bull_trend = evaluate_index_trend(bull_df, "NIFTY")
    assert bull_trend.regime == "BULLISH"

    # Alternating bearish drift so RSI stays in 28..48 (60% down / 40% up)
    bear_closes = [24500.0]
    for i in range(1, 30):
        step = -6.0 if i % 2 == 1 else 4.0
        bear_closes.append(bear_closes[-1] + step)
    bear_df = pd.DataFrame(
        [
            {
                "timestamp": start + timedelta(minutes=5 * i),
                "open": px + 4.0,
                "high": px + 6.0,
                "low": px - 6.0,
                "close": px,
            }
            for i, px in enumerate(bear_closes)
        ]
    )
    bear_trend = evaluate_index_trend(bear_df, "NIFTY")
    assert bear_trend.regime == "BEARISH"
    assert 28.0 <= bear_trend.rsi_14 <= 48.0
    assert bear_trend.recommended_fno_action == "BUY_PE"


def test_credentials_env_and_trade_book_csv(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    creds = AngelCredentials(
        api_key="key123",
        client_id="C12345",
        password="9999",
        totp_secret="JBSWY3DPEHPK3PXP",
    )
    save_credentials(creds, env_file)
    loaded = load_credentials(env_file)
    assert loaded == creds

    csv_file = tmp_path / "trades_log.csv"
    tb = TradeBookManager(csv_file)
    row = tb.record_entry(
        mode="PAPER",
        symbol="SBIN",
        tradingsymbol="SBIN-EQ",
        token="3045",
        qty=100,
        entry_price=800.0,
        stop_loss=796.0,
        target=808.0,
    )
    events = tb.sync_open_trades_with_ltp("PAPER", {"SBIN": 808.5})
    assert len(events) == 1
    assert events[0]["reason"] == "TARGET HIT"
    assert events[0]["pnl"] < 800.0 and events[0]["pnl"] > 700.0  # Net of NSE intraday charges

    summary = tb.compute_today_summary("PAPER", {"SBIN": 808.5})
    assert summary["gross_realized_pnl"] == 800.0
    assert summary["total_charges"] > 0
    assert summary["realized_pnl"] == round(800.0 - summary["total_charges"], 2)
    assert summary["trades_used"] == 1
    assert summary["open_positions_count"] == 0
    assert row["trade_id"] in tb.get_today_trades("PAPER")["trade_id"].values


def test_broker_rate_limit_retry_raises_rate_limit_error() -> None:
    broker = AngelOneBroker()
    broker.is_connected = True
    broker.client = object()  # Dummy handle for testing retry wrapper only

    def always_rate_limited():
        return {"status": False, "message": "Access denied because of exceeding access rate"}

    broker._order_limiter.min_interval_sec = 0.0
    with pytest.raises(RateLimitError):
        broker._call_with_retry(broker._order_limiter, always_rate_limited, max_retries=2)


def test_streamlit_app_renders_and_executes_paper_trade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    app_path = Path(__file__).resolve().parent.parent / "app.py"

    # Simulate 10:15 AM IST inside active window
    monkeypatch.setattr("risk_manager.now_ist", lambda: datetime(2026, 11, 25, 10, 15, tzinfo=IST))

    at = AppTest.from_file(str(app_path), default_timeout=20)
    at.run()
    assert not at.exception
    assert at.toggle(key="paper_mode").value is True
    assert at.selectbox(key="universe_preset").value == "nifty50"
    assert len(at.metric) == 4
    assert at.metric[3].value == "ACTIVE"

    # Seed one valid BUY signal evaluation into session state and verify Execute Trade + Panic Exit
    sample_signal = StockEvaluation(
        symbol="SBIN",
        tradingsymbol="SBIN-EQ",
        token="3045",
        candle_time="2026-11-25 10:15 IST",
        price=800.0,
        vwap=795.0,
        ema_9=798.0,
        ema_21=797.0,
        prev_ema_9=796.5,
        prev_ema_21=797.0,
        rsi_14=61.5,
        volume=250000,
        avg_volume_20=100000,
        volume_ratio=2.5,
        cond_price_above_vwap=True,
        cond_ema_bullish_crossover=True,
        cond_rsi_and_volume=True,
        is_buy_signal=True,
        stop_loss=796.0,
        target=808.0,
        qty=100,
        sl_distance=4.0,
        expected_profit=800.0,
    )
    monkeypatch.setattr(
        AngelOneBroker,
        "fetch_batch_ltp",
        lambda self, stocks, require_smartapi=False: {"SBIN": 802.0},
    )
    at.session_state["evaluations"] = [sample_signal]
    at.session_state["ltp_map"] = {"SBIN": 802.0}
    at.run()
    assert not at.exception

    at.button(key="execute_trade_btn").click().run()
    assert not at.exception
    assert at.metric[2].value == "1"
    assert (tmp_path / "trades_log.csv").exists()

    # Verify Panic Exit squares off open paper position
    at.button(key="panic_exit_btn").click().run()
    assert not at.exception
    assert at.metric[2].value == "1"

    # Verify Backtest Last 5 Days button executes cleanly without error
    sample_hist = _make_candles_for_crossover(rsi_target_mode="sweet_spot", volume_spike=True, above_vwap=True)
    monkeypatch.setattr(
        AngelOneBroker,
        "resolve_universe_tokens",
        lambda self, universe_preset="nifty50", custom_symbols="": [
            {"symbol": "SBIN", "tradingsymbol": "SBIN-EQ", "token": "3045", "yahoo_symbol": "SBIN.NS"}
        ],
    )
    monkeypatch.setattr(
        AngelOneBroker,
        "fetch_candles_in_batches",
        lambda self, stocks, use_smartapi, days_back=5, batch_size=10, call_delay_sec=0.35, on_progress=None: (
            {"SBIN": sample_hist},
            {},
            {"Mock"},
        ),
    )
    at.button(key="backtest_btn").click().run()
    assert not at.exception
    assert at.session_state["backtest_result"] is not None

    # Verify Optional F&O page loads cleanly via st.navigation (including Stock Options OPTSTK/FUTSTK selector)
    at.switch_page("app_pages/fno_trading.py").run()
    assert not at.exception
    assert len(at.metric) == 4
    assert "SBIN" in at.selectbox(key="fno_underlying").options


def test_stock_options_and_futures_contract_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import broker as broker_mod

    monkeypatch.setattr(broker_mod, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(broker_mod, "SCRIP_CACHE_FILE", tmp_path / "nse_eq.json")
    monkeypatch.setattr(broker_mod, "NFO_CACHE_FILE", tmp_path / "nfo_v2.json")

    sample_master = [
        {"exch_seg": "NSE", "symbol": "SBIN-EQ", "token": "3045"},
        {
            "exch_seg": "NFO",
            "instrumenttype": "OPTSTK",
            "name": "SBIN",
            "symbol": "SBIN26DEC26800CE",
            "token": "55001",
            "expiry": "26DEC2026",
            "strike": "80000",
            "lotsize": "750",
        },
        {
            "exch_seg": "NFO",
            "instrumenttype": "OPTSTK",
            "name": "SBIN",
            "symbol": "SBIN26DEC26800PE",
            "token": "55002",
            "expiry": "26DEC2026",
            "strike": "80000",
            "lotsize": "750",
        },
        {
            "exch_seg": "NFO",
            "instrumenttype": "FUTSTK",
            "name": "SBIN",
            "symbol": "SBIN26DEC26FUT",
            "token": "55003",
            "expiry": "26DEC2026",
            "strike": "0",
            "lotsize": "750",
        },
    ]
    broker_mod._cache_scrip_master_subsets(sample_master)

    b = AngelOneBroker()
    ce_list = b.get_fno_contracts("SBIN", "OPTSTK", spot_price=805.0, option_type="CE", max_strikes=5)
    assert len(ce_list) == 1
    assert ce_list[0]["tradingsymbol"] == "SBIN26DEC26800CE"
    assert ce_list[0]["lotsize"] == 750

    fut_list = b.get_fno_contracts("SBIN", "FUTSTK")
    assert len(fut_list) == 1
    assert fut_list[0]["tradingsymbol"] == "SBIN26DEC26FUT"
