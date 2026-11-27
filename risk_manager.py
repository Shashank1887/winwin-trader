"""Hardcoded intraday risk guardrails and position sizing for the assistant."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from typing import Literal
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Hardcoded guardrails mandated by strategy rules (cannot be relaxed by UI settings)
DAILY_PROFIT_TARGET = 1000.0
DAILY_MAX_LOSS = 800.0
MAX_SL_PER_TRADE = 400.0
MAX_TRADES_PER_DAY = 2
ACTIVE_WINDOW_START = dt_time(9, 20)
ACTIVE_WINDOW_END = dt_time(11, 30)
NSE_TICK_SIZE = 0.05

SystemStatus = Literal["ACTIVE", "GOAL LOCKED", "KILL-SWITCH LOCKED"]
WindowPreset = Literal["morning", "split", "full"]
SegmentType = Literal["EQ_INTRADAY", "NFO_OPTIONS", "NFO_FUTURES"]

# Conservative safety caps for the optional F&O page (Option Buying / 1-Lot Futures only)
FNO_MAX_LOTS_CAP = 2
FNO_DAILY_PROFIT_TARGET = 2500.0
FNO_DAILY_MAX_LOSS = 1500.0
FNO_MAX_SL_PER_TRADE = 1500.0
FNO_MAX_TRADES_PER_DAY = 2

TRADING_WINDOWS: dict[str, dict[str, object]] = {
    "morning": {
        "label": "09:20 – 11:30 IST (Morning)",
        "ranges": [(dt_time(9, 20), dt_time(11, 30))],
    },
    "split": {
        "label": "09:20–11:30 & 13:45–14:55 IST",
        "ranges": [(dt_time(9, 20), dt_time(11, 30)), (dt_time(13, 45), dt_time(14, 55))],
    },
    "full": {
        "label": "09:20 – 15:00 IST (Full Day)",
        "ranges": [(dt_time(9, 20), dt_time(15, 0))],
    },
}


def get_window_label(window_preset: str = "morning") -> str:
    """Return human-readable label for a trading window preset."""
    entry = TRADING_WINDOWS.get(window_preset, TRADING_WINDOWS["morning"])
    return str(entry["label"])


def get_window_end_time(window_preset: str = "morning") -> dt_time:
    """Return the final closing time of the selected trading window."""
    entry = TRADING_WINDOWS.get(window_preset, TRADING_WINDOWS["morning"])
    ranges: list[tuple[dt_time, dt_time]] = entry["ranges"]  # type: ignore[assignment]
    return ranges[-1][1]


def round_to_tick(price: float, tick: float = NSE_TICK_SIZE) -> float:
    """Round a price to the nearest valid NSE tick size (0.05)."""
    if tick <= 0:
        return round(price, 2)
    return round(round(price / tick) * tick, 2)


def now_ist() -> datetime:
    """Return current timezone-aware datetime in Asia/Kolkata."""
    return datetime.now(IST)


def is_within_active_window(
    current_dt: datetime | None = None,
    window_preset: WindowPreset = "morning",
) -> bool:
    """Return True when current IST time falls inside the selected trading window."""
    dt = current_dt.astimezone(IST) if current_dt else now_ist()
    current_t = dt.time()
    entry = TRADING_WINDOWS.get(window_preset, TRADING_WINDOWS["morning"])
    ranges: list[tuple[dt_time, dt_time]] = entry["ranges"]  # type: ignore[assignment]
    return any(start <= current_t <= end for start, end in ranges)


@dataclass(frozen=True)
class RiskConfig:
    """Capital and risk parameters strictly bounded by hardcoded guardrails."""

    capital: float = 50000.0
    daily_profit_target: float = DAILY_PROFIT_TARGET
    daily_max_loss: float = DAILY_MAX_LOSS
    max_sl_per_trade: float = MAX_SL_PER_TRADE
    max_trades_per_day: int = MAX_TRADES_PER_DAY
    window_preset: WindowPreset = "morning"

    def __post_init__(self) -> None:
        object.__setattr__(self, "capital", max(1000.0, float(self.capital)))
        object.__setattr__(
            self,
            "daily_profit_target",
            min(max(100.0, float(self.daily_profit_target)), DAILY_PROFIT_TARGET),
        )
        object.__setattr__(
            self,
            "daily_max_loss",
            min(max(100.0, float(self.daily_max_loss)), DAILY_MAX_LOSS),
        )
        object.__setattr__(
            self,
            "max_sl_per_trade",
            min(max(50.0, float(self.max_sl_per_trade)), MAX_SL_PER_TRADE),
        )
        object.__setattr__(
            self,
            "max_trades_per_day",
            min(max(1, int(self.max_trades_per_day)), MAX_TRADES_PER_DAY),
        )
        if self.window_preset not in TRADING_WINDOWS:
            object.__setattr__(self, "window_preset", "morning")


@dataclass(frozen=True)
class PositionSizing:
    """Calculated intraday order sizing and risk-reward levels."""

    entry_price: float
    stop_loss: float
    target: float
    sl_distance: float
    qty: int
    max_loss_rupees: float
    target_profit_rupees: float
    required_capital: float


@dataclass(frozen=True)
class RiskSnapshot:
    """Live evaluation of daily risk limits, trade quota, and system lock state."""

    realized_pnl: float
    unrealized_pnl: float
    net_pnl: float
    trades_used: int
    max_trades: int
    open_positions_count: int
    system_status: SystemStatus
    status_reason: str
    within_active_window: bool
    can_trade: bool
    goal_progress_pct: float
    should_auto_square_off: bool


def calculate_position_sizing(
    entry_price: float,
    stop_loss_price: float,
    capital: float,
    max_sl_rupees: float = MAX_SL_PER_TRADE,
) -> PositionSizing:
    """Calculate share quantity for a fixed stop-loss rupee budget and 1:2 target."""
    entry = round_to_tick(float(entry_price))
    sl = round_to_tick(float(stop_loss_price))
    sl_budget = min(float(max_sl_rupees), MAX_SL_PER_TRADE)

    if entry <= 0 or sl <= 0 or sl >= entry:
        return PositionSizing(
            entry_price=entry,
            stop_loss=sl,
            target=entry,
            sl_distance=0.0,
            qty=0,
            max_loss_rupees=0.0,
            target_profit_rupees=0.0,
            required_capital=0.0,
        )

    sl_distance = round(entry - sl, 2)
    if sl_distance <= 0:
        return PositionSizing(
            entry_price=entry,
            stop_loss=sl,
            target=entry,
            sl_distance=0.0,
            qty=0,
            max_loss_rupees=0.0,
            target_profit_rupees=0.0,
            required_capital=0.0,
        )

    risk_qty = int(math.floor(sl_budget / sl_distance))
    capital_qty = int(math.floor(float(capital) / entry)) if capital > 0 else 0
    qty = max(0, min(risk_qty, capital_qty))

    target = round_to_tick(entry + 2.0 * sl_distance)
    max_loss = round(qty * sl_distance, 2)
    target_profit = round(qty * (target - entry), 2)
    required_capital = round(qty * entry, 2)

    return PositionSizing(
        entry_price=entry,
        stop_loss=sl,
        target=target,
        sl_distance=sl_distance,
        qty=qty,
        max_loss_rupees=max_loss,
        target_profit_rupees=target_profit,
        required_capital=required_capital,
    )


def evaluate_risk_state(
    realized_pnl: float,
    unrealized_pnl: float,
    trades_used: int,
    open_positions_count: int,
    config: RiskConfig | None = None,
    current_dt: datetime | None = None,
) -> RiskSnapshot:
    """Evaluate hardcoded profit target, kill-switch, trade cap, and time window."""
    cfg = config or RiskConfig()
    net_pnl = round(realized_pnl + unrealized_pnl, 2)
    within_window = is_within_active_window(current_dt, window_preset=cfg.window_preset)
    window_label = get_window_label(cfg.window_preset)
    goal_pct = max(0.0, min(100.0, (realized_pnl / cfg.daily_profit_target) * 100.0))

    if realized_pnl <= -cfg.daily_max_loss or net_pnl <= -cfg.daily_max_loss:
        return RiskSnapshot(
            realized_pnl=round(realized_pnl, 2),
            unrealized_pnl=round(unrealized_pnl, 2),
            net_pnl=net_pnl,
            trades_used=trades_used,
            max_trades=cfg.max_trades_per_day,
            open_positions_count=open_positions_count,
            system_status="KILL-SWITCH LOCKED",
            status_reason=f"Daily loss limit (-₹{cfg.daily_max_loss:,.0f}) reached.",
            within_active_window=within_window,
            can_trade=False,
            goal_progress_pct=goal_pct,
            should_auto_square_off=open_positions_count > 0,
        )

    if realized_pnl >= cfg.daily_profit_target:
        return RiskSnapshot(
            realized_pnl=round(realized_pnl, 2),
            unrealized_pnl=round(unrealized_pnl, 2),
            net_pnl=net_pnl,
            trades_used=trades_used,
            max_trades=cfg.max_trades_per_day,
            open_positions_count=open_positions_count,
            system_status="GOAL LOCKED",
            status_reason=f"Daily profit target (+₹{cfg.daily_profit_target:,.0f}) achieved.",
            within_active_window=within_window,
            can_trade=False,
            goal_progress_pct=100.0,
            should_auto_square_off=False,
        )

    if trades_used >= cfg.max_trades_per_day:
        return RiskSnapshot(
            realized_pnl=round(realized_pnl, 2),
            unrealized_pnl=round(unrealized_pnl, 2),
            net_pnl=net_pnl,
            trades_used=trades_used,
            max_trades=cfg.max_trades_per_day,
            open_positions_count=open_positions_count,
            system_status="ACTIVE",
            status_reason=f"Daily trade cap ({trades_used}/{cfg.max_trades_per_day}) reached.",
            within_active_window=within_window,
            can_trade=False,
            goal_progress_pct=goal_pct,
            should_auto_square_off=False,
        )

    if not within_window:
        return RiskSnapshot(
            realized_pnl=round(realized_pnl, 2),
            unrealized_pnl=round(unrealized_pnl, 2),
            net_pnl=net_pnl,
            trades_used=trades_used,
            max_trades=cfg.max_trades_per_day,
            open_positions_count=open_positions_count,
            system_status="ACTIVE",
            status_reason=f"Outside active window ({window_label}).",
            within_active_window=False,
            can_trade=False,
            goal_progress_pct=goal_pct,
            should_auto_square_off=False,
        )

    return RiskSnapshot(
        realized_pnl=round(realized_pnl, 2),
        unrealized_pnl=round(unrealized_pnl, 2),
        net_pnl=net_pnl,
        trades_used=trades_used,
        max_trades=cfg.max_trades_per_day,
        open_positions_count=open_positions_count,
        system_status="ACTIVE",
        status_reason="Active window open and risk within guardrails.",
        within_active_window=True,
        can_trade=True,
        goal_progress_pct=goal_pct,
        should_auto_square_off=False,
    )


def can_execute_trade(
    snapshot: RiskSnapshot,
    symbol: str,
    open_symbols: set[str],
    qty: int,
    sl_distance: float,
    config: RiskConfig | None = None,
) -> tuple[bool, str]:
    """Validate all hardcoded guardrails before placing any paper or live order."""
    cfg = config or RiskConfig()

    if snapshot.system_status == "KILL-SWITCH LOCKED":
        return False, snapshot.status_reason
    if snapshot.system_status == "GOAL LOCKED":
        return False, snapshot.status_reason
    if not snapshot.within_active_window:
        return False, f"Scanning and trading are restricted to {get_window_label(cfg.window_preset)}."
    if snapshot.trades_used >= cfg.max_trades_per_day:
        return False, f"Max {cfg.max_trades_per_day} trades per day already used."
    if symbol in open_symbols:
        return False, f"An open position already exists for {symbol}."
    if qty <= 0 or sl_distance <= 0:
        return False, "Calculated quantity is 0 (check stop-loss distance vs. available capital)."
    if round(qty * sl_distance, 2) > cfg.max_sl_per_trade + 1e-6:
        return False, f"Trade risk exceeds max ₹{cfg.max_sl_per_trade:,.0f} stop-loss per trade."

    return True, "Eligible for execution."


@dataclass(frozen=True)
class FnoRiskConfig:
    """Safe, strictly bounded risk parameters for the optional F&O trading page."""

    capital: float = 50000.0
    max_lots: int = 1
    sl_points: float = 15.0
    daily_profit_target: float = FNO_DAILY_PROFIT_TARGET
    daily_max_loss: float = FNO_DAILY_MAX_LOSS
    max_trades_per_day: int = FNO_MAX_TRADES_PER_DAY
    window_preset: WindowPreset = "morning"

    def __post_init__(self) -> None:
        object.__setattr__(self, "capital", max(5000.0, float(self.capital)))
        object.__setattr__(self, "max_lots", min(max(1, int(self.max_lots)), FNO_MAX_LOTS_CAP))
        object.__setattr__(self, "sl_points", min(max(5.0, float(self.sl_points)), 50.0))
        object.__setattr__(
            self,
            "daily_profit_target",
            min(max(500.0, float(self.daily_profit_target)), 5000.0),
        )
        object.__setattr__(
            self,
            "daily_max_loss",
            min(max(500.0, float(self.daily_max_loss)), 2500.0),
        )
        object.__setattr__(
            self,
            "max_trades_per_day",
            min(max(1, int(self.max_trades_per_day)), FNO_MAX_TRADES_PER_DAY),
        )
        if self.window_preset not in TRADING_WINDOWS:
            object.__setattr__(self, "window_preset", "morning")


def compute_trailing_stop_loss(
    entry_price: float,
    current_sl: float,
    target_price: float,
    ltp: float,
) -> tuple[float, str]:
    """Compute stepped Trailing SL: Break-Even at +1R, Profit-Lock (+0.75R) at +1.5R."""
    if entry_price <= 0 or target_price <= entry_price or ltp <= entry_price:
        return round(current_sl, 2), ""

    risk_dist = round((target_price - entry_price) / 2.0, 2)
    if risk_dist <= 0:
        return round(current_sl, 2), ""

    candidate = current_sl
    stage = ""
    if ltp >= entry_price + 1.5 * risk_dist:
        lock_px = round_to_tick(entry_price + 0.75 * risk_dist)
        if lock_px > candidate:
            candidate = lock_px
            stage = "TRAIL PROFIT LOCK (+0.75R)"
    elif ltp >= entry_price + 1.0 * risk_dist:
        be_px = round_to_tick(entry_price)
        if be_px > candidate:
            candidate = be_px
            stage = "TRAIL BREAK-EVEN (1R)"

    return round(candidate, 2), stage


def calculate_nse_charges(
    segment: SegmentType,
    buy_value: float,
    sell_value: float,
) -> dict[str, float]:
    """Estimate realistic Angel One / NSE intraday brokerage, STT, taxes, and GST."""
    buy_v = max(0.0, float(buy_value))
    sell_v = max(0.0, float(sell_value))
    turnover = buy_v + sell_v
    if turnover <= 0:
        return {
            "brokerage": 0.0,
            "stt": 0.0,
            "exchange_txn": 0.0,
            "sebi": 0.0,
            "stamp_duty": 0.0,
            "gst": 0.0,
            "total": 0.0,
        }

    if segment == "NFO_OPTIONS":
        brokerage = (20.0 if buy_v > 0 else 0.0) + (20.0 if sell_v > 0 else 0.0)
        stt = 0.001 * sell_v
        exchange_txn = 0.0003503 * turnover
        stamp_duty = 0.00003 * buy_v
    elif segment == "NFO_FUTURES":
        brokerage = (min(20.0, 0.0003 * buy_v) if buy_v > 0 else 0.0) + (
            min(20.0, 0.0003 * sell_v) if sell_v > 0 else 0.0
        )
        stt = 0.0002 * sell_v
        exchange_txn = 0.0000173 * turnover
        stamp_duty = 0.00002 * buy_v
    else:  # EQ_INTRADAY
        brokerage = (min(20.0, 0.0003 * buy_v) if buy_v > 0 else 0.0) + (
            min(20.0, 0.0003 * sell_v) if sell_v > 0 else 0.0
        )
        stt = 0.00025 * sell_v
        exchange_txn = 0.0000297 * turnover
        stamp_duty = 0.00003 * buy_v

    sebi = 0.000001 * turnover
    gst = 0.18 * (brokerage + exchange_txn + sebi)
    total = round(brokerage + stt + exchange_txn + sebi + stamp_duty + gst, 2)
    return {
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exchange_txn": round(exchange_txn, 2),
        "sebi": round(sebi, 2),
        "stamp_duty": round(stamp_duty, 2),
        "gst": round(gst, 2),
        "total": total,
    }
