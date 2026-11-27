"""FastAPI mobile API server tests."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_server
from broker import AngelOneBroker, TradeBookManager
from risk_manager import IST
from strategy import IndexTrend, StockEvaluation


def test_api_health_and_auth_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api_server, "MOBILE_API_TOKEN", "test-secret-token")
    monkeypatch.setattr(api_server.runtime, "trade_book", TradeBookManager(tmp_path / "trades_log.csv"))
    monkeypatch.setattr(api_server.runtime, "evaluations", [])
    monkeypatch.setattr(api_server.runtime, "ltp_map", {})

    client = TestClient(api_server.app)

    home = client.get("/")
    assert home.status_code == 200
    assert "WinWin Intraday Trader" in home.text

    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.json()["display"] == "standalone"

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    unauth = client.get("/api/state")
    assert unauth.status_code == 401

    bad_auth = client.get("/api/state", headers={"Authorization": "Bearer wrong"})
    assert bad_auth.status_code == 401

    ok_auth = client.get("/api/state", headers={"Authorization": "Bearer test-secret-token"})
    assert ok_auth.status_code == 200
    payload = ok_auth.json()
    assert payload["mode"] == "PAPER"
    assert payload["risk_snapshot"]["max_trades"] == 0

    custom_cap = client.get("/api/state?max_trades=5", headers={"Authorization": "Bearer test-secret-token"})
    assert custom_cap.status_code == 200
    assert custom_cap.json()["risk_snapshot"]["max_trades"] == 5


def test_api_execute_and_panic_exit_paper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api_server, "MOBILE_API_TOKEN", "test-secret-token")
    monkeypatch.setattr("risk_manager.now_ist", lambda: datetime(2026, 11, 25, 10, 15, tzinfo=IST))
    monkeypatch.setattr(api_server, "now_ist", lambda: datetime(2026, 11, 25, 10, 15, tzinfo=IST))

    tb = TradeBookManager(tmp_path / "trades_log.csv")
    monkeypatch.setattr(api_server.runtime, "trade_book", tb)
    monkeypatch.setattr(api_server.runtime, "broker", AngelOneBroker())
    monkeypatch.setattr(api_server.runtime, "ltp_map", {"SBIN": 802.0})
    monkeypatch.setattr(api_server.runtime, "executed_signal_candles", set())
    monkeypatch.setattr(
        AngelOneBroker,
        "fetch_batch_ltp",
        lambda self, stocks, require_smartapi=False: {"SBIN": 802.0},
    )
    monkeypatch.setattr(
        api_server.runtime,
        "evaluations",
        [
            StockEvaluation(
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
        ],
    )

    client = TestClient(api_server.app)
    headers = {"Authorization": "Bearer test-secret-token"}

    exec_res = client.post("/api/execute", json={"mode": "PAPER", "symbol": "SBIN"}, headers=headers)
    assert exec_res.status_code == 200
    assert exec_res.json()["risk_snapshot"]["trades_used"] == 1
    assert exec_res.json()["risk_snapshot"]["open_positions_count"] == 1

    panic_res = client.post("/api/panic-exit", json={"mode": "PAPER"}, headers=headers)
    assert panic_res.status_code == 200
    assert panic_res.json()["risk_snapshot"]["open_positions_count"] == 0
    assert panic_res.json()["trades"][0]["status"] == "CLOSED"
    assert panic_res.json()["trades"][0]["exit_reason"] == "PANIC EXIT"


def test_api_automation_configure_and_tick_auto_executes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api_server, "MOBILE_API_TOKEN", "test-secret-token")
    monkeypatch.setattr("risk_manager.now_ist", lambda: datetime(2026, 11, 25, 10, 15, tzinfo=IST))
    monkeypatch.setattr(api_server, "now_ist", lambda: datetime(2026, 11, 25, 10, 15, tzinfo=IST))

    tb = TradeBookManager(tmp_path / "trades_log.csv")
    monkeypatch.setattr(api_server.runtime, "trade_book", tb)
    monkeypatch.setattr(api_server.runtime, "broker", AngelOneBroker())
    monkeypatch.setattr(api_server.runtime, "ltp_map", {})
    monkeypatch.setattr(api_server.runtime, "executed_signal_candles", set())
    monkeypatch.setattr(api_server.runtime, "last_scan_epoch", 0.0)
    monkeypatch.setattr(api_server.runtime, "last_ltp_epoch", 0.0)

    sample_sig = StockEvaluation(
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
        api_server,
        "_perform_network_scan",
        lambda mode, risk_cfg, universe_preset="top15", custom_symbols="": (
            [sample_sig],
            {"SBIN": 800.0},
            {"NSE Live"},
            None,
        ),
    )
    monkeypatch.setattr(
        AngelOneBroker,
        "fetch_batch_ltp",
        lambda self, stocks, require_smartapi=False: {"SBIN": 800.0},
    )

    client = TestClient(api_server.app)
    headers = {"Authorization": "Bearer test-secret-token"}

    cfg_res = client.post(
        "/api/automation",
        json={
            "enabled": True,
            "mode": "PAPER",
            "scan_interval_sec": 60,
            "ltp_interval_sec": 15,
            "auto_execute": True,
            "auto_square_off_eod": False,
            "window_preset": "full",
            "universe_preset": "nifty50",
        },
        headers=headers,
    )
    assert cfg_res.status_code == 200
    assert cfg_res.json()["automation"]["enabled"] is True
    assert cfg_res.json()["automation"]["universe_preset"] == "nifty50"
    assert cfg_res.json()["guardrails"]["window_preset"] == "full"

    api_server.run_automation_tick()
    state_res = client.get("/api/state?mode=PAPER", headers=headers)
    assert state_res.status_code == 200
    data = state_res.json()
    assert data["risk_snapshot"]["trades_used"] == 1
    assert data["trades"][0]["symbol"] == "SBIN"

    # Disable automation cleanly after test
    client.post("/api/automation", json={"enabled": False, "mode": "PAPER"}, headers=headers)


def test_api_sell_short_execute_and_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api_server, "MOBILE_API_TOKEN", "test-secret-token")
    monkeypatch.setattr("risk_manager.now_ist", lambda: datetime(2026, 11, 25, 10, 15, tzinfo=IST))
    monkeypatch.setattr(api_server, "now_ist", lambda: datetime(2026, 11, 25, 10, 15, tzinfo=IST))

    tb = TradeBookManager(tmp_path / "trades_log.csv")
    monkeypatch.setattr(api_server.runtime, "trade_book", tb)
    monkeypatch.setattr(api_server.runtime, "broker", AngelOneBroker())
    monkeypatch.setattr(api_server.runtime, "ltp_map", {"TCS": 3992.0})
    monkeypatch.setattr(api_server.runtime, "index_trend", None)
    monkeypatch.setattr(api_server.runtime, "executed_signal_candles", set())
    monkeypatch.setattr(
        AngelOneBroker,
        "fetch_batch_ltp",
        lambda self, stocks, require_smartapi=False: {"TCS": 4000.0},
    )
    monkeypatch.setattr(
        api_server.runtime,
        "evaluations",
        [
            StockEvaluation(
                symbol="TCS",
                tradingsymbol="TCS-EQ",
                token="11536",
                candle_time="2026-11-25 10:15 IST",
                price=4000.0,
                vwap=4020.0,
                ema_9=3995.0,
                ema_21=4010.0,
                prev_ema_9=4002.0,
                prev_ema_21=4012.0,
                rsi_14=41.0,
                volume=150000,
                avg_volume_20=100000,
                volume_ratio=1.5,
                cond_price_above_vwap=False,
                cond_ema_bullish_crossover=False,
                cond_rsi_and_volume=True,
                is_buy_signal=False,
                stop_loss=4010.0,
                target=3980.0,
                qty=40,
                sl_distance=10.0,
                expected_profit=800.0,
                side="SELL",
                is_sell_signal=True,
                cond_price_below_vwap=True,
                cond_ema_bearish=True,
                cond_sell_rsi_and_volume=True,
            )
        ],
    )

    client = TestClient(api_server.app)
    headers = {"Authorization": "Bearer test-secret-token"}

    res = client.post("/api/execute", json={"mode": "PAPER", "symbol": "TCS"}, headers=headers)
    assert res.status_code == 200
    payload = res.json()
    assert payload["trades"][0]["side"] == "SELL"
    assert payload["trades"][0]["pnl"] > 0  # entry 3992->price fell or short gains on lower ltp

    panic = client.post("/api/panic-exit", json={"mode": "PAPER"}, headers=headers)
    assert panic.status_code == 200
    assert panic.json()["trades"][0]["status"] == "CLOSED"


def test_api_nifty_filter_and_fno_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api_server, "MOBILE_API_TOKEN", "test-secret-token")
    monkeypatch.setattr("risk_manager.now_ist", lambda: datetime(2026, 11, 25, 10, 15, tzinfo=IST))
    monkeypatch.setattr(api_server, "now_ist", lambda: datetime(2026, 11, 25, 10, 15, tzinfo=IST))

    tb = TradeBookManager(tmp_path / "trades_log.csv")
    monkeypatch.setattr(api_server.runtime, "trade_book", tb)
    monkeypatch.setattr(api_server.runtime, "broker", AngelOneBroker())
    monkeypatch.setattr(api_server.runtime, "ltp_map", {})
    monkeypatch.setattr(api_server.runtime, "nifty_trend_filter", True)
    monkeypatch.setattr(
        api_server.runtime,
        "index_trend",
        IndexTrend("NIFTY", "2026-11-25 10:15 IST", 24000.0, 24010.0, 24050.0, 42.0, "BEARISH", "BUY_PE"),
    )
    monkeypatch.setattr(
        api_server.runtime,
        "evaluations",
        [
            StockEvaluation(
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
        ],
    )

    client = TestClient(api_server.app)
    headers = {"Authorization": "Bearer test-secret-token"}

    # Equity BUY blocked when NIFTY 50 is BEARISH and filter is ON
    blocked = client.post("/api/execute", json={"mode": "PAPER", "symbol": "SBIN"}, headers=headers)
    assert blocked.status_code == 400
    assert "NIFTY 50 Trend Filter" in blocked.json()["detail"]

    # F&O PAPER trade flow (execute + panic exit)
    fno_exec = client.post(
        "/api/fno/execute",
        json={
            "mode": "PAPER",
            "tradingsymbol": "NIFTY29DEC2624000PE",
            "token": "99999",
            "qty": 65,
            "entry_price": 120.0,
            "sl_points": 15.0,
            "segment": "NFO_OPTIONS",
            "risk_ack": True,
        },
        headers=headers,
    )
    assert fno_exec.status_code == 200
    assert fno_exec.json()["summary"]["trades_used"] == 1

    fno_panic = client.post("/api/fno/panic-exit", json={"mode": "PAPER"}, headers=headers)
    assert fno_panic.status_code == 200
    assert fno_panic.json()["summary"]["open_positions_count"] == 0
    assert fno_panic.json()["trades"][0]["status"] == "CLOSED"

    # Verify /api/fno/analyze accepts Stock Options (OPTSTK) cleanly
    start = datetime(2026, 11, 25, 9, 15, tzinfo=IST)
    import pandas as pd
    from datetime import timedelta

    mock_df = pd.DataFrame(
        [
            {"timestamp": start + timedelta(minutes=5 * i), "open": 800 + i, "high": 802 + i, "low": 799 + i, "close": 801 + i, "volume": 10000}
            for i in range(30)
        ]
    )
    monkeypatch.setattr(api_server.runtime.broker, "fetch_fno_underlying_candles", lambda *a, **kw: (mock_df, "Mock"))
    monkeypatch.setattr(
        api_server.runtime.broker,
        "get_fno_contracts",
        lambda underlying, instrument_type, **kw: [{"tradingsymbol": "SBIN26DEC26820CE", "token": "55010", "expiry": "26DEC2026", "strike": 820.0, "lotsize": 750}],
    )
    stk_an = client.post(
        "/api/fno/analyze",
        json={"mode": "PAPER", "underlying": "SBIN", "instrument_type": "OPTSTK", "option_type": "CE"},
        headers=headers,
    )
    assert stk_an.status_code == 200
    assert stk_an.json()["contracts"][0]["tradingsymbol"] == "SBIN26DEC26820CE"


def test_telegram_config_and_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from notifier import TelegramConfig, load_telegram_config, save_telegram_config, send_telegram_message

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api_server, "MOBILE_API_TOKEN", "test-secret-token")

    env_path = tmp_path / ".env"
    cfg = TelegramConfig(bot_token="bot123456:ABC_xyz", chat_id="987654", enabled=True)
    save_telegram_config(cfg, env_path)
    loaded = load_telegram_config(env_path)
    assert loaded.bot_token == "123456:ABC_xyz"
    assert loaded.chat_id == "987654"
    assert loaded.enabled is True

    class _Resp:
        status_code = 200
        content = b'{"ok": true}'

        @staticmethod
        def json():
            return {"ok": True}

    monkeypatch.setattr("notifier.requests.post", lambda *a, **kw: _Resp())
    ok, msg = send_telegram_message("hello", cfg=loaded)
    assert ok and "sent" in msg.lower()

    client = TestClient(api_server.app)
    headers = {"Authorization": "Bearer test-secret-token"}
    save_res = client.post(
        "/api/telegram/config",
        json={"bot_token": "123456:ABC_xyz", "chat_id": "987654", "enabled": True},
        headers=headers,
    )
    assert save_res.status_code == 200
    assert save_res.json()["configured"] is True

    test_res = client.post(
        "/api/telegram/test",
        json={"bot_token": "", "chat_id": "", "enabled": True},
        headers=headers,
    )
    assert test_res.status_code == 200
    assert test_res.json()["ok"] is True
