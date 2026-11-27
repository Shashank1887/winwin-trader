"""Angel One SmartAPI broker wrapper, live market data feed, and CSV trade logger."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyotp
import requests
import yfinance as yf
from dotenv import dotenv_values, set_key
from SmartApi.smartConnect import SmartConnect
from SmartApi.smartExceptions import DataException, NetworkException, SmartAPIException, TokenException

from risk_manager import (
    IST,
    SegmentType,
    calculate_nse_charges,
    compute_trailing_stop_loss,
    now_ist,
)
from strategy import NIFTY50_ALL_SYMBOLS, NIFTY50_LIQUID_UNIVERSE, UniversePreset, parse_custom_symbols

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
CACHE_DIR = Path(".cache")
SCRIP_CACHE_FILE = CACHE_DIR / "nse_eq_tokens.json"
NFO_CACHE_FILE = CACHE_DIR / "nfo_contracts.json"

INDEX_UNIVERSE: dict[str, dict[str, str]] = {
    "NIFTY": {"symbol": "NIFTY", "tradingsymbol": "Nifty 50", "token": "99926000", "yahoo_symbol": "^NSEI"},
    "BANKNIFTY": {"symbol": "BANKNIFTY", "tradingsymbol": "Nifty Bank", "token": "99926009", "yahoo_symbol": "^NSEBANK"},
    "FINNIFTY": {"symbol": "FINNIFTY", "tradingsymbol": "Nifty Fin Service", "token": "99926037", "yahoo_symbol": "NIFTY_FIN_SERVICE.NS"},
}

TRADES_LOG_COLUMNS = [
    "trade_id",
    "date_ist",
    "timestamp",
    "mode",
    "segment",
    "exchange",
    "symbol",
    "tradingsymbol",
    "token",
    "side",
    "qty",
    "entry_price",
    "stop_loss",
    "target",
    "exit_price",
    "exit_time",
    "exit_reason",
    "status",
    "gross_pnl",
    "charges",
    "realized_pnl",
    "entry_order_id",
    "sl_order_id",
    "target_order_id",
    "notes",
]


class BrokerError(Exception):
    """Base error raised by AngelOneBroker."""


class BrokerAuthError(BrokerError):
    """Raised when SmartAPI login or session renewal fails."""


class RateLimitError(BrokerError):
    """Raised when Angel One API rate limits persist after retries."""


class BrokerOrderError(BrokerError):
    """Raised when live order placement or square-off fails."""


@dataclass
class AngelCredentials:
    """Angel One SmartAPI login credentials."""

    api_key: str = ""
    client_id: str = ""
    password: str = ""
    totp_secret: str = ""

    def is_complete(self) -> bool:
        return all(
            [
                self.api_key.strip(),
                self.client_id.strip(),
                self.password.strip(),
                self.totp_secret.strip(),
            ]
        )


def load_credentials(env_path: str | Path = ".env") -> AngelCredentials:
    """Load Angel One credentials from .env file or process environment."""
    path = Path(env_path)
    values = dotenv_values(path) if path.exists() else {}
    return AngelCredentials(
        api_key=str(values.get("ANGEL_API_KEY") or os.getenv("ANGEL_API_KEY", "")).strip(),
        client_id=str(values.get("ANGEL_CLIENT_ID") or os.getenv("ANGEL_CLIENT_ID", "")).strip(),
        password=str(values.get("ANGEL_PASSWORD") or os.getenv("ANGEL_PASSWORD", "")).strip(),
        totp_secret=str(values.get("ANGEL_TOTP_SECRET") or os.getenv("ANGEL_TOTP_SECRET", "")).strip(),
    )


def save_credentials(creds: AngelCredentials, env_path: str | Path = ".env") -> None:
    """Persist Angel One credentials into .env."""
    path = Path(env_path)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    set_key(str(path), "ANGEL_API_KEY", creds.api_key.strip())
    set_key(str(path), "ANGEL_CLIENT_ID", creds.client_id.strip())
    set_key(str(path), "ANGEL_PASSWORD", creds.password.strip())
    set_key(str(path), "ANGEL_TOTP_SECRET", creds.totp_secret.strip())


class RateLimiter:
    """Thread-safe minimum-interval rate limiter for REST API calls."""

    def __init__(self, min_interval_sec: float = 0.4) -> None:
        self.min_interval_sec = min_interval_sec
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self.min_interval_sec:
                time.sleep(self.min_interval_sec - elapsed)
            self._last_call = time.monotonic()


def _is_rate_limit_message(message: str) -> bool:
    lowered = message.lower()
    return any(
        token in lowered
        for token in (
            "exceeding access rate",
            "rate limit",
            "too many requests",
            "429",
            "access denied because of exceeding",
        )
    )


class AngelOneBroker:
    """Manages SmartAPI authentication, rate-limited market data, and live orders."""

    def __init__(self, credentials: AngelCredentials | None = None) -> None:
        self.credentials = credentials or AngelCredentials()
        self.client: SmartConnect | None = None
        self.is_connected: bool = False
        self.profile_name: str = ""
        self.last_error: str = ""
        self._candle_limiter = RateLimiter(min_interval_sec=0.4)
        self._quote_limiter = RateLimiter(min_interval_sec=1.05)
        self._order_limiter = RateLimiter(min_interval_sec=0.2)

    def connect(self, credentials: AngelCredentials | None = None) -> dict[str, Any]:
        """Authenticate with Angel One SmartAPI using password/PIN and TOTP."""
        if credentials is not None:
            self.credentials = credentials
        if not self.credentials.is_complete():
            raise BrokerAuthError("API Key, Client ID, Password/PIN, and TOTP Secret are all required.")

        try:
            totp_code = pyotp.TOTP(self.credentials.totp_secret.replace(" ", "")).now()
        except Exception as exc:
            raise BrokerAuthError(f"Invalid TOTP secret: {exc}") from exc

        try:
            smart_api = SmartConnect(api_key=self.credentials.api_key)
            session = smart_api.generateSession(
                self.credentials.client_id,
                self.credentials.password,
                totp_code,
            )
        except Exception as exc:
            self.is_connected = False
            raise BrokerAuthError(f"SmartAPI login error: {exc}") from exc

        if not isinstance(session, dict) or not session.get("status"):
            msg = session.get("message", "Unknown authentication failure") if isinstance(session, dict) else str(session)
            self.is_connected = False
            raise BrokerAuthError(f"SmartAPI login rejected: {msg}")

        self.client = smart_api
        self.is_connected = True
        self.last_error = ""
        data = session.get("data") or {}
        self.profile_name = str(data.get("name") or self.credentials.client_id)
        return session

    def disconnect(self) -> None:
        """Terminate the SmartAPI session cleanly if connected."""
        if self.client and self.is_connected and self.credentials.client_id:
            try:
                self.client.terminateSession(self.credentials.client_id)
            except Exception:
                pass
        self.client = None
        self.is_connected = False
        self.profile_name = ""

    def _call_with_retry(self, limiter: RateLimiter, fn, *args, max_retries: int = 3, **kwargs) -> Any:
        """Execute a SmartAPI call with rate-limit throttling and exponential backoff."""
        if not self.client or not self.is_connected:
            raise BrokerAuthError("Angel One SmartAPI session is not connected.")

        last_exc: Exception | None = None
        for attempt in range(max_retries):
            limiter.wait()
            try:
                resp = fn(*args, **kwargs)
                if isinstance(resp, dict) and resp.get("status") is False:
                    msg = str(resp.get("message") or resp.get("errorcode") or "API returned status=False")
                    error_code = str(resp.get("errorcode") or "")
                    if _is_rate_limit_message(msg):
                        if attempt < max_retries - 1:
                            time.sleep(1.5 * (2**attempt))
                            continue
                        raise RateLimitError(f"Angel One rate limit exceeded: {msg}")
                    if error_code in {"AG8001", "AG8002", "AB8050"} or "token" in msg.lower():
                        # Try transparent re-authentication once
                        if attempt == 0 and self.credentials.is_complete():
                            self.connect(self.credentials)
                            continue
                        raise BrokerAuthError(f"Session expired or invalid token: {msg}")
                return resp
            except TokenException as exc:
                last_exc = exc
                if attempt == 0 and self.credentials.is_complete():
                    self.connect(self.credentials)
                    continue
                raise BrokerAuthError(f"SmartAPI token error: {exc}") from exc
            except (NetworkException, DataException, SmartAPIException, requests.RequestException) as exc:
                last_exc = exc
                msg = str(exc)
                if _is_rate_limit_message(msg):
                    if attempt < max_retries - 1:
                        time.sleep(1.5 * (2**attempt))
                        continue
                    raise RateLimitError(f"Angel One rate limit exceeded: {msg}") from exc
                if attempt < max_retries - 1:
                    time.sleep(1.0 * (2**attempt))
                    continue
                raise BrokerError(f"SmartAPI request failed: {msg}") from exc

        raise BrokerError(f"SmartAPI call failed after retries: {last_exc}")

    def resolve_universe_tokens(
        self,
        universe_preset: UniversePreset = "top15",
        custom_symbols: str = "",
    ) -> list[dict[str, str]]:
        """Resolve NSE equity tokens for Top 15, Full Nifty 50, or Custom Watchlist."""
        token_map = _load_cached_scrip_tokens()
        fallback_lookup = {s["symbol"]: dict(s) for s in NIFTY50_LIQUID_UNIVERSE}

        if universe_preset == "nifty50":
            symbols = list(NIFTY50_ALL_SYMBOLS)
        elif universe_preset == "custom":
            symbols = parse_custom_symbols(custom_symbols) or [s["symbol"] for s in NIFTY50_LIQUID_UNIVERSE]
        else:
            symbols = [s["symbol"] for s in NIFTY50_LIQUID_UNIVERSE]

        resolved: list[dict[str, str]] = []
        for sym in symbols:
            if sym in fallback_lookup:
                entry = dict(fallback_lookup[sym])
            elif sym == "TATAMOTORS":
                entry = {
                    "symbol": "TATAMOTORS",
                    "tradingsymbol": "TMPV-EQ",
                    "token": token_map.get("TMPV-EQ", "3456"),
                    "yahoo_symbol": "TMPV.NS",
                }
            else:
                tsym = f"{sym}-EQ"
                entry = {
                    "symbol": sym,
                    "tradingsymbol": tsym,
                    "token": token_map.get(tsym, ""),
                    "yahoo_symbol": f"{sym}.NS",
                }

            if entry["tradingsymbol"] in token_map:
                entry["token"] = token_map[entry["tradingsymbol"]]
            elif f"{entry['symbol']}-EQ" in token_map:
                entry["tradingsymbol"] = f"{entry['symbol']}-EQ"
                entry["token"] = token_map[entry["tradingsymbol"]]
            resolved.append(entry)

        return resolved

    def fetch_5m_candles(
        self,
        stock: dict[str, str],
        use_smartapi: bool,
        days_back: int = 5,
    ) -> tuple[pd.DataFrame, str]:
        """Fetch real 5-minute OHLCV candles from SmartAPI or live NSE feed (Paper fallback)."""
        if use_smartapi:
            if not self.is_connected:
                raise BrokerAuthError("Connect Angel One SmartAPI in the sidebar to fetch SmartAPI candles.")
            return self._fetch_smartapi_candles(stock, days_back=days_back), "Angel One SmartAPI"

        if self.is_connected:
            try:
                return self._fetch_smartapi_candles(stock, days_back=days_back), "Angel One SmartAPI"
            except RateLimitError:
                raise
            except Exception:
                # In Paper Mode, if SmartAPI candle endpoint fails transiently, fall back to live NSE feed
                pass

        return self._fetch_yfinance_candles(stock, days_back=days_back), "NSE Live (yfinance)"

    def _fetch_smartapi_candles(self, stock: dict[str, str], days_back: int = 5) -> pd.DataFrame:
        to_dt = now_ist()
        from_dt = to_dt - timedelta(days=days_back)
        params = {
            "exchange": "NSE",
            "symboltoken": stock["token"],
            "interval": "FIVE_MINUTE",
            "fromdate": from_dt.strftime("%Y-%m-%d 09:15"),
            "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
        }
        resp = self._call_with_retry(self._candle_limiter, self.client.getCandleData, params)
        if not isinstance(resp, dict) or not resp.get("status"):
            msg = resp.get("message", "No candle data") if isinstance(resp, dict) else str(resp)
            raise BrokerError(f"{stock['symbol']}: {msg}")

        rows = resp.get("data") or []
        if not rows:
            raise BrokerError(f"{stock['symbol']}: SmartAPI returned 0 candles.")

        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df

    def _fetch_yfinance_candles(self, stock: dict[str, str], days_back: int = 5) -> pd.DataFrame:
        ticker = stock.get("yahoo_symbol") or f"{stock['symbol']}.NS"
        hist = yf.Ticker(ticker).history(period=f"{max(days_back, 5)}d", interval="5m", auto_adjust=False)
        if hist is None or hist.empty:
            raise BrokerError(f"{stock['symbol']}: Live 5-minute NSE data unavailable.")

        hist = hist.reset_index()
        ts_col = "Datetime" if "Datetime" in hist.columns else hist.columns[0]
        df = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(hist[ts_col]),
                "open": hist["Open"],
                "high": hist["High"],
                "low": hist["Low"],
                "close": hist["Close"],
                "volume": hist["Volume"],
            }
        )
        return df

    def fetch_batch_ltp(
        self,
        stocks: list[dict[str, str]],
        require_smartapi: bool = False,
    ) -> dict[str, float]:
        """Fetch live Last Traded Prices in batch (SmartAPI getMarketData first, else NSE live)."""
        if not stocks:
            return {}

        if self.is_connected and self.client is not None:
            try:
                valid = [s for s in stocks if s.get("token")]
                token_to_sym = {s["token"]: s["symbol"] for s in valid}
                prices: dict[str, float] = {}
                for i in range(0, len(valid), 50):
                    chunk_tokens = [s["token"] for s in valid[i : i + 50]]
                    resp = self._call_with_retry(
                        self._quote_limiter,
                        self.client.getMarketData,
                        "LTP",
                        {"NSE": chunk_tokens},
                    )
                    if isinstance(resp, dict) and resp.get("status") and resp.get("data"):
                        for item in resp["data"].get("fetched") or []:
                            tok = str(item.get("symbolToken") or "")
                            ltp = item.get("ltp")
                            if tok in token_to_sym and ltp is not None:
                                prices[token_to_sym[tok]] = float(ltp)
                if prices:
                    return prices
            except RateLimitError:
                if require_smartapi:
                    raise
            except Exception as exc:
                if require_smartapi:
                    raise BrokerError(f"Failed to fetch SmartAPI LTPs: {exc}") from exc

        if require_smartapi:
            raise BrokerAuthError("Angel One SmartAPI session is required for live LTP quotes.")

        # Paper mode live NSE fallback using yfinance latest intraday bars
        prices: dict[str, float] = {}
        tickers = [s.get("yahoo_symbol") or f"{s['symbol']}.NS" for s in stocks]
        try:
            raw = yf.download(
                tickers=tickers,
                period="1d",
                interval="1m",
                group_by="ticker",
                progress=False,
                threads=True,
            )
            if raw is not None and not raw.empty:
                for stock, ysym in zip(stocks, tickers):
                    try:
                        sub = raw if len(tickers) == 1 else raw[ysym]
                        closes = sub["Close"].dropna()
                        if not closes.empty:
                            prices[stock["symbol"]] = round(float(closes.iloc[-1]), 2)
                    except Exception:
                        continue
        except Exception:
            pass

        # Individual fallback for any missing ticker
        for stock in stocks:
            if stock["symbol"] not in prices:
                try:
                    ysym = stock.get("yahoo_symbol") or f"{stock['symbol']}.NS"
                    fast = yf.Ticker(ysym).fast_info
                    last_px = getattr(fast, "last_price", None)
                    if last_px:
                        prices[stock["symbol"]] = round(float(last_px), 2)
                except Exception:
                    continue

        return prices

    def fetch_index_spot_candles(
        self,
        index_name: str = "NIFTY",
        use_smartapi: bool = False,
        days_back: int = 5,
    ) -> tuple[pd.DataFrame, str]:
        """Fetch 5-minute spot candles for NIFTY / BANKNIFTY / FINNIFTY."""
        idx_info = INDEX_UNIVERSE.get(index_name.upper(), INDEX_UNIVERSE["NIFTY"])
        return self.fetch_5m_candles(idx_info, use_smartapi=use_smartapi, days_back=days_back)

    def fetch_nfo_ltp(
        self,
        contracts: list[dict[str, Any]],
        require_smartapi: bool = False,
    ) -> dict[str, float]:
        """Fetch live LTPs for NFO option/future contracts via Angel One SmartAPI."""
        if not contracts or not self.is_connected or self.client is None:
            if require_smartapi:
                raise BrokerAuthError("Connect Angel One SmartAPI to fetch live NFO option/future prices.")
            return {}

        valid = [c for c in contracts if c.get("token")]
        tok_to_sym = {str(c["token"]): str(c["tradingsymbol"]) for c in valid}
        prices: dict[str, float] = {}
        try:
            for i in range(0, len(valid), 50):
                chunk = [str(c["token"]) for c in valid[i : i + 50]]
                resp = self._call_with_retry(
                    self._quote_limiter,
                    self.client.getMarketData,
                    "LTP",
                    {"NFO": chunk},
                )
                if isinstance(resp, dict) and resp.get("status") and resp.get("data"):
                    for item in resp["data"].get("fetched") or []:
                        tok = str(item.get("symbolToken") or "")
                        ltp = item.get("ltp")
                        if tok in tok_to_sym and ltp is not None:
                            prices[tok_to_sym[tok]] = float(ltp)
        except Exception as exc:
            if require_smartapi:
                raise BrokerError(f"Failed to fetch NFO LTPs: {exc}") from exc
        return prices

    def get_fno_contracts(
        self,
        underlying: str = "NIFTY",
        instrument_type: str = "OPTIDX",
        spot_price: float | None = None,
        option_type: str = "CE",
        max_strikes: int = 5,
    ) -> list[dict[str, Any]]:
        """Return nearest-expiry NFO Option or Future contracts from Angel One scrip master."""
        contracts = _load_cached_nfo_contracts()
        today_date = now_ist().date()

        filtered = []
        for row in contracts:
            if row.get("name") != underlying.upper():
                continue
            if row.get("instrumenttype") != instrument_type.upper():
                continue
            try:
                exp_dt = datetime.strptime(str(row["expiry"]), "%d%b%Y").date()
            except Exception:
                continue
            if exp_dt < today_date:
                continue
            if instrument_type.upper() == "OPTIDX":
                sym = str(row["tradingsymbol"])
                if option_type in ("CE", "PE") and not sym.endswith(option_type):
                    continue
            filtered.append({**row, "_exp_dt": exp_dt})

        if not filtered:
            return []

        nearest_exp = min(r["_exp_dt"] for r in filtered)
        nearest_rows = [r for r in filtered if r["_exp_dt"] == nearest_exp]

        if instrument_type.upper() == "FUTIDX":
            filtered.sort(key=lambda r: r["_exp_dt"])
            return [{k: v for k, v in r.items() if k != "_exp_dt"} for r in filtered[:2]]

        if spot_price and spot_price > 0:
            nearest_rows.sort(key=lambda r: abs(float(r["strike"]) - float(spot_price)))
            picked = nearest_rows[:max_strikes]
            picked.sort(key=lambda r: float(r["strike"]))
        else:
            nearest_rows.sort(key=lambda r: float(r["strike"]))
            mid = len(nearest_rows) // 2
            half = max(1, max_strikes // 2)
            picked = nearest_rows[max(0, mid - half) : mid + half + 1]

        return [{k: v for k, v in r.items() if k != "_exp_dt"} for r in picked]

    def place_live_buy_with_protection(
        self,
        symbol: str,
        tradingsymbol: str,
        token: str,
        qty: int,
        entry_price_hint: float,
        stop_loss: float,
        target: float,
        exchange: str = "NSE",
        producttype: str = "INTRADAY",
    ) -> dict[str, Any]:
        """Place a live MARKET BUY order plus protective SL-M and Target LIMIT exits."""
        if not self.is_connected or self.client is None:
            raise BrokerAuthError("Connect Angel One SmartAPI before placing live orders.")

        entry_params = {
            "variety": "NORMAL",
            "tradingsymbol": tradingsymbol,
            "symboltoken": str(token),
            "transactiontype": "BUY",
            "exchange": exchange,
            "ordertype": "MARKET",
            "producttype": producttype,
            "duration": "DAY",
            "price": "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(qty),
        }
        entry_resp = self._call_with_retry(self._order_limiter, self.client.placeOrderFullResponse, entry_params)
        if not isinstance(entry_resp, dict) or not entry_resp.get("status"):
            msg = entry_resp.get("message", "Unknown order rejection") if isinstance(entry_resp, dict) else str(entry_resp)
            raise BrokerOrderError(f"Live BUY order failed for {tradingsymbol}: {msg}")

        entry_order_id = str((entry_resp.get("data") or {}).get("orderid") or "")
        if not entry_order_id:
            raise BrokerOrderError(f"Live BUY order returned no order ID for {tradingsymbol}.")

        fill_price = self._resolve_fill_price(entry_order_id, fallback=entry_price_hint)

        sl_order_id = ""
        target_order_id = ""
        notes: list[str] = []

        # Protective Stop-Loss Market order
        try:
            sl_params = {
                "variety": "STOPLOSS",
                "tradingsymbol": tradingsymbol,
                "symboltoken": str(token),
                "transactiontype": "SELL",
                "exchange": exchange,
                "ordertype": "STOPLOSS_MARKET",
                "producttype": producttype,
                "duration": "DAY",
                "price": "0",
                "triggerprice": f"{stop_loss:.2f}",
                "squareoff": "0",
                "stoploss": "0",
                "quantity": str(qty),
            }
            sl_resp = self._call_with_retry(self._order_limiter, self.client.placeOrderFullResponse, sl_params)
            if isinstance(sl_resp, dict) and sl_resp.get("status"):
                sl_order_id = str((sl_resp.get("data") or {}).get("orderid") or "")
            else:
                notes.append(f"SL order warning: {sl_resp.get('message') if isinstance(sl_resp, dict) else sl_resp}")
        except Exception as exc:
            notes.append(f"SL order warning: {exc}")

        # Target Limit Sell order
        try:
            tgt_params = {
                "variety": "NORMAL",
                "tradingsymbol": tradingsymbol,
                "symboltoken": str(token),
                "transactiontype": "SELL",
                "exchange": exchange,
                "ordertype": "LIMIT",
                "producttype": producttype,
                "duration": "DAY",
                "price": f"{target:.2f}",
                "squareoff": "0",
                "stoploss": "0",
                "quantity": str(qty),
            }
            tgt_resp = self._call_with_retry(self._order_limiter, self.client.placeOrderFullResponse, tgt_params)
            if isinstance(tgt_resp, dict) and tgt_resp.get("status"):
                target_order_id = str((tgt_resp.get("data") or {}).get("orderid") or "")
            else:
                notes.append(
                    f"Target order warning: {tgt_resp.get('message') if isinstance(tgt_resp, dict) else tgt_resp}"
                )
        except Exception as exc:
            notes.append(f"Target order warning: {exc}")

        return {
            "entry_order_id": entry_order_id,
            "sl_order_id": sl_order_id,
            "target_order_id": target_order_id,
            "fill_price": fill_price,
            "notes": "; ".join(notes),
        }

    def _resolve_fill_price(self, order_id: str, fallback: float) -> float:
        """Query SmartAPI orderBook briefly to fetch the actual average traded price."""
        try:
            time.sleep(0.35)
            book = self._call_with_retry(self._order_limiter, self.client.orderBook)
            if isinstance(book, dict) and book.get("status") and book.get("data"):
                for order in book["data"]:
                    if str(order.get("orderid")) == str(order_id):
                        status = str(order.get("status") or "").lower()
                        if status in {"rejected", "cancelled"}:
                            reason = order.get("text") or status
                            raise BrokerOrderError(f"Entry order {order_id} {status}: {reason}")
                        avg_px = float(order.get("averageprice") or 0.0)
                        if avg_px > 0:
                            return round(avg_px, 2)
        except BrokerOrderError:
            raise
        except Exception:
            pass
        return round(float(fallback), 2)

    def modify_live_sl_order(self, trade: dict[str, Any], new_sl: float) -> bool:
        """Modify an open protective SL-M order trigger price for Trailing Stop-Loss."""
        sl_order_id = str(trade.get("sl_order_id") or "").strip()
        if not sl_order_id or not self.is_connected or self.client is None:
            return False
        exchange = str(trade.get("exchange") or ("NFO" if str(trade.get("segment", "")).startswith("NFO") else "NSE"))
        params = {
            "variety": "STOPLOSS",
            "orderid": sl_order_id,
            "ordertype": "STOPLOSS_MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": "0",
            "triggerprice": f"{new_sl:.2f}",
            "quantity": str(int(trade["qty"])),
            "tradingsymbol": str(trade["tradingsymbol"]),
            "symboltoken": str(trade["token"]),
            "exchange": exchange,
        }
        try:
            resp = self._call_with_retry(self._order_limiter, self.client.modifyOrder, params)
            return bool(isinstance(resp, dict) and resp.get("status"))
        except Exception:
            return False

    def sync_live_orders(self, trade_book: "TradeBookManager") -> list[dict[str, Any]]:
        """Reconcile LIVE open trades against Angel One orderBook and cancel opposite OCO leg."""
        if not self.is_connected or self.client is None:
            return []

        today_df = trade_book.get_today_trades(mode="LIVE")
        open_df = today_df[today_df["status"] == "OPEN"] if not today_df.empty else pd.DataFrame()
        if open_df.empty:
            return []

        try:
            book = self._call_with_retry(self._order_limiter, self.client.orderBook)
        except Exception:
            return []

        if not isinstance(book, dict) or not book.get("status") or not book.get("data"):
            return []

        orders_by_id = {str(o.get("orderid")): o for o in book["data"] if o.get("orderid")}
        events: list[dict[str, Any]] = []

        for _, row in open_df.iterrows():
            trade_id = str(row["trade_id"])
            sym = str(row["symbol"])
            sl_id = str(row.get("sl_order_id") or "").strip()
            tgt_id = str(row.get("target_order_id") or "").strip()

            sl_order = orders_by_id.get(sl_id)
            tgt_order = orders_by_id.get(tgt_id)

            if sl_order and str(sl_order.get("status") or "").lower() == "complete":
                avg_px = float(sl_order.get("averageprice") or row["stop_loss"])
                if tgt_id:
                    try:
                        self._call_with_retry(self._order_limiter, self.client.cancelOrder, tgt_id, "NORMAL")
                    except Exception:
                        pass
                pnl = trade_book.close_trade(trade_id, avg_px, "SL HIT (LIVE OCO)")
                events.append({"trade_id": trade_id, "symbol": sym, "reason": "SL HIT (LIVE OCO)", "pnl": pnl})
            elif tgt_order and str(tgt_order.get("status") or "").lower() == "complete":
                avg_px = float(tgt_order.get("averageprice") or row["target"])
                if sl_id:
                    try:
                        self._call_with_retry(self._order_limiter, self.client.cancelOrder, sl_id, "STOPLOSS")
                    except Exception:
                        pass
                pnl = trade_book.close_trade(trade_id, avg_px, "TARGET HIT (LIVE OCO)")
                events.append({"trade_id": trade_id, "symbol": sym, "reason": "TARGET HIT (LIVE OCO)", "pnl": pnl})

        return events

    def square_off_live_trade(self, trade: dict[str, Any], ltp_hint: float) -> dict[str, Any]:
        """Cancel pending SL/Target orders and exit an open live intraday position at MARKET."""
        if not self.is_connected or self.client is None:
            raise BrokerAuthError("Connect Angel One SmartAPI to square off live positions.")

        for order_id, variety in (
            (str(trade.get("sl_order_id") or "").strip(), "STOPLOSS"),
            (str(trade.get("target_order_id") or "").strip(), "NORMAL"),
        ):
            if order_id:
                try:
                    self._call_with_retry(self._order_limiter, self.client.cancelOrder, order_id, variety)
                except Exception:
                    pass

        exchange = str(trade.get("exchange") or ("NFO" if str(trade.get("segment", "")).startswith("NFO") else "NSE"))
        exit_params = {
            "variety": "NORMAL",
            "tradingsymbol": str(trade["tradingsymbol"]),
            "symboltoken": str(trade["token"]),
            "transactiontype": "SELL",
            "exchange": exchange,
            "ordertype": "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(int(trade["qty"])),
        }
        resp = self._call_with_retry(self._order_limiter, self.client.placeOrderFullResponse, exit_params)
        if not isinstance(resp, dict) or not resp.get("status"):
            msg = resp.get("message", "Exit order rejected") if isinstance(resp, dict) else str(resp)
            raise BrokerOrderError(f"Panic exit failed for {trade['tradingsymbol']}: {msg}")

        exit_order_id = str((resp.get("data") or {}).get("orderid") or "")
        exit_price = self._resolve_fill_price(exit_order_id, fallback=ltp_hint)
        return {"exit_order_id": exit_order_id, "exit_price": exit_price}


def _load_cached_scrip_tokens() -> dict[str, str]:
    """Load cached NSE -EQ tokens or refresh from Angel One OpenAPIScripMaster.json if stale."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if SCRIP_CACHE_FILE.exists():
        age_sec = time.time() - SCRIP_CACHE_FILE.stat().st_mtime
        if age_sec < 86400:
            try:
                return json.loads(SCRIP_CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass

    try:
        resp = requests.get(SCRIP_MASTER_URL, timeout=15)
        if resp.status_code == 200:
            master = resp.json()
            _cache_scrip_master_subsets(master)
            if SCRIP_CACHE_FILE.exists():
                return json.loads(SCRIP_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass

    return {}


def _load_cached_nfo_contracts() -> list[dict[str, Any]]:
    """Load cached NFO index Option/Future contracts or refresh from OpenAPIScripMaster.json."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if NFO_CACHE_FILE.exists():
        age_sec = time.time() - NFO_CACHE_FILE.stat().st_mtime
        if age_sec < 86400:
            try:
                return json.loads(NFO_CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass

    try:
        resp = requests.get(SCRIP_MASTER_URL, timeout=15)
        if resp.status_code == 200:
            master = resp.json()
            _cache_scrip_master_subsets(master)
            if NFO_CACHE_FILE.exists():
                return json.loads(NFO_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass

    return []


def _cache_scrip_master_subsets(master: list[dict[str, Any]]) -> None:
    """Save compact NSE -EQ and NFO OPTIDX/FUTIDX subsets from OpenAPIScripMaster.json."""
    token_map = {
        str(row["symbol"]): str(row["token"])
        for row in master
        if row.get("exch_seg") == "NSE" and str(row.get("symbol", "")).endswith("-EQ")
    }
    if token_map:
        SCRIP_CACHE_FILE.write_text(json.dumps(token_map), encoding="utf-8")

    nfo_rows = []
    for row in master:
        if (
            row.get("exch_seg") == "NFO"
            and row.get("instrumenttype") in ("OPTIDX", "FUTIDX")
            and row.get("name") in ("NIFTY", "BANKNIFTY", "FINNIFTY")
        ):
            try:
                strike_raw = float(row.get("strike") or 0.0)
                strike_val = round(strike_raw / 100.0, 2) if strike_raw > 0 else 0.0
                lotsize_val = int(float(row.get("lotsize") or 1))
            except Exception:
                continue
            nfo_rows.append(
                {
                    "name": str(row["name"]),
                    "instrumenttype": str(row["instrumenttype"]),
                    "tradingsymbol": str(row["symbol"]),
                    "token": str(row["token"]),
                    "expiry": str(row.get("expiry") or ""),
                    "strike": strike_val,
                    "lotsize": lotsize_val,
                }
            )
    if nfo_rows:
        NFO_CACHE_FILE.write_text(json.dumps(nfo_rows), encoding="utf-8")


class TradeBookManager:
    """Thread-safe CSV persistence for paper and live intraday trades (`trades_log.csv`)."""

    def __init__(self, csv_path: str | Path = "trades_log.csv") -> None:
        self.csv_path = Path(csv_path)
        self._lock = threading.Lock()
        self._ensure_file()

    def _ensure_file(self) -> None:
        if not self.csv_path.exists():
            pd.DataFrame(columns=TRADES_LOG_COLUMNS).to_csv(self.csv_path, index=False)

    def _read_csv_unlocked(self) -> pd.DataFrame:
        str_cols = {
            "trade_id": "string",
            "date_ist": "string",
            "timestamp": "string",
            "mode": "string",
            "segment": "string",
            "exchange": "string",
            "symbol": "string",
            "tradingsymbol": "string",
            "token": "string",
            "side": "string",
            "exit_time": "string",
            "exit_reason": "string",
            "status": "string",
            "entry_order_id": "string",
            "sl_order_id": "string",
            "target_order_id": "string",
            "notes": "string",
        }
        df = pd.read_csv(self.csv_path, dtype=str_cols)
        for col in TRADES_LOG_COLUMNS:
            if col not in df.columns:
                if col == "segment":
                    df[col] = "EQ_INTRADAY"
                elif col == "exchange":
                    df[col] = "NSE"
                elif col in str_cols:
                    df[col] = ""
                else:
                    df[col] = 0.0 if col in ("gross_pnl", "charges", "realized_pnl") else np.nan
        for col in str_cols:
            df[col] = df[col].fillna("").astype("string")
        df["segment"] = df["segment"].replace("", "EQ_INTRADAY")
        df["exchange"] = df["exchange"].replace("", "NSE")
        return df[TRADES_LOG_COLUMNS]

    def read_all(self) -> pd.DataFrame:
        """Read all logged trades safely."""
        with self._lock:
            self._ensure_file()
            return self._read_csv_unlocked()

    def _write_all(self, df: pd.DataFrame) -> None:
        df[TRADES_LOG_COLUMNS].to_csv(self.csv_path, index=False)

    def get_today_trades(
        self,
        mode: str | None = None,
        date_str: str | None = None,
        segment: SegmentType | tuple[SegmentType, ...] | None = None,
    ) -> pd.DataFrame:
        """Return today's IST trades, optionally filtered by PAPER/LIVE mode and segment(s)."""
        today = date_str or now_ist().strftime("%Y-%m-%d")
        df = self.read_all()
        if df.empty:
            return df
        filtered = df[df["date_ist"] == today].copy()
        if mode is not None:
            filtered = filtered[filtered["mode"] == mode].copy()
        if segment is not None:
            allowed = (segment,) if isinstance(segment, str) else tuple(segment)
            filtered = filtered[filtered["segment"].isin(allowed)].copy()
        return filtered.reset_index(drop=True)

    def record_entry(
        self,
        mode: str,
        symbol: str,
        tradingsymbol: str,
        token: str,
        qty: int,
        entry_price: float,
        stop_loss: float,
        target: float,
        entry_order_id: str = "",
        sl_order_id: str = "",
        target_order_id: str = "",
        notes: str = "",
        segment: SegmentType = "EQ_INTRADAY",
        exchange: str = "NSE",
    ) -> dict[str, Any]:
        """Log a new open intraday BUY trade to trades_log.csv."""
        ts = now_ist()
        row = {
            "trade_id": f"{mode[:1]}-{ts.strftime('%H%M%S')}-{uuid.uuid4().hex[:4].upper()}",
            "date_ist": ts.strftime("%Y-%m-%d"),
            "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": mode,
            "segment": segment,
            "exchange": exchange,
            "symbol": symbol,
            "tradingsymbol": tradingsymbol,
            "token": str(token),
            "side": "BUY",
            "qty": int(qty),
            "entry_price": round(float(entry_price), 2),
            "stop_loss": round(float(stop_loss), 2),
            "target": round(float(target), 2),
            "exit_price": None,
            "exit_time": "",
            "exit_reason": "",
            "status": "OPEN",
            "gross_pnl": 0.0,
            "charges": 0.0,
            "realized_pnl": 0.0,
            "entry_order_id": entry_order_id or f"PAPER-{uuid.uuid4().hex[:6].upper()}",
            "sl_order_id": sl_order_id,
            "target_order_id": target_order_id,
            "notes": notes,
        }
        with self._lock:
            self._ensure_file()
            df = self._read_csv_unlocked()
            new_row_df = pd.DataFrame([row])
            df = new_row_df if df.empty else pd.concat([df, new_row_df], ignore_index=True)
            self._write_all(df)
        return row

    def update_trade_sl(self, trade_id: str, new_sl: float, note: str = "") -> None:
        """Persist a trailed Stop-Loss level for an OPEN trade."""
        with self._lock:
            self._ensure_file()
            df = self._read_csv_unlocked()
            mask = (df["trade_id"] == trade_id) & (df["status"] == "OPEN")
            if not mask.any():
                return
            idx = df.index[mask][0]
            df.at[idx, "stop_loss"] = round(float(new_sl), 2)
            if note:
                existing = str(df.at[idx, "notes"] or "")
                df.at[idx, "notes"] = f"{existing}; {note}".strip("; ")
            self._write_all(df)

    def close_trade(self, trade_id: str, exit_price: float, exit_reason: str) -> float:
        """Close an OPEN trade at exit_price and persist gross P&L, charges, and net realized P&L."""
        ts = now_ist().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._ensure_file()
            df = self._read_csv_unlocked()
            mask = (df["trade_id"] == trade_id) & (df["status"] == "OPEN")
            if not mask.any():
                return 0.0
            idx = df.index[mask][0]
            qty = int(df.at[idx, "qty"])
            entry_price = float(df.at[idx, "entry_price"])
            segment = str(df.at[idx, "segment"] or "EQ_INTRADAY")
            exit_px = round(float(exit_price), 2)
            gross_pnl = round((exit_px - entry_price) * qty, 2)
            charges_info = calculate_nse_charges(
                segment=segment,  # type: ignore[arg-type]
                buy_value=entry_price * qty,
                sell_value=exit_px * qty,
            )
            total_charges = charges_info["total"]
            net_pnl = round(gross_pnl - total_charges, 2)

            df.at[idx, "exit_price"] = exit_px
            df.at[idx, "exit_time"] = ts
            df.at[idx, "exit_reason"] = exit_reason
            df.at[idx, "status"] = "CLOSED"
            df.at[idx, "gross_pnl"] = gross_pnl
            df.at[idx, "charges"] = total_charges
            df.at[idx, "realized_pnl"] = net_pnl
            self._write_all(df)
            return net_pnl

    def sync_open_trades_with_ltp(
        self,
        mode: str,
        ltp_map: dict[str, float],
        auto_close_paper_sl_target: bool = True,
        enable_trailing_sl: bool = True,
        broker: AngelOneBroker | None = None,
        segment: SegmentType | tuple[SegmentType, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Evaluate open positions for SL/Target hits, Trailing SL updates, and Live OCO sync."""
        events: list[dict[str, Any]] = []
        if mode == "LIVE" and broker is not None:
            events.extend(broker.sync_live_orders(self))

        today_df = self.get_today_trades(mode=mode, segment=segment)
        if today_df.empty:
            return events

        open_df = today_df[today_df["status"] == "OPEN"]
        for _, row in open_df.iterrows():
            sym = str(row["symbol"])
            tsym = str(row["tradingsymbol"])
            ltp = ltp_map.get(sym) or ltp_map.get(tsym)
            if ltp is None or ltp <= 0:
                continue

            entry_px = float(row["entry_price"])
            sl = float(row["stop_loss"])
            tgt = float(row["target"])
            trade_id = str(row["trade_id"])

            if mode == "PAPER" and auto_close_paper_sl_target:
                if ltp <= sl:
                    exit_px = min(sl, ltp) if ltp < sl * 0.995 else sl
                    pnl = self.close_trade(trade_id, exit_px, "SL HIT")
                    events.append({"trade_id": trade_id, "symbol": sym, "reason": "SL HIT", "pnl": pnl})
                    continue
                if ltp >= tgt:
                    exit_px = max(tgt, ltp) if ltp > tgt * 1.005 else tgt
                    pnl = self.close_trade(trade_id, exit_px, "TARGET HIT")
                    events.append({"trade_id": trade_id, "symbol": sym, "reason": "TARGET HIT", "pnl": pnl})
                    continue

            if enable_trailing_sl:
                new_sl, trail_stage = compute_trailing_stop_loss(entry_px, sl, tgt, ltp)
                if new_sl > sl:
                    if mode == "LIVE" and broker is not None:
                        broker.modify_live_sl_order(row.to_dict(), new_sl)
                    self.update_trade_sl(trade_id, new_sl, note=f"{trail_stage} -> ₹{new_sl:.2f}")
                    events.append(
                        {
                            "trade_id": trade_id,
                            "symbol": sym,
                            "reason": f"{trail_stage} (SL ₹{new_sl:.2f})",
                            "pnl": 0.0,
                        }
                    )

        return events

    def compute_today_summary(
        self,
        mode: str,
        ltp_map: dict[str, float],
        segment: SegmentType | tuple[SegmentType, ...] | None = "EQ_INTRADAY",
    ) -> dict[str, Any]:
        """Compute net realized P&L (after charges), gross P&L, charges, unrealized P&L, and open symbols."""
        today_df = self.get_today_trades(mode=mode, segment=segment)
        if today_df.empty:
            return {
                "realized_pnl": 0.0,
                "gross_realized_pnl": 0.0,
                "total_charges": 0.0,
                "unrealized_pnl": 0.0,
                "net_pnl": 0.0,
                "trades_used": 0,
                "open_positions_count": 0,
                "open_symbols": set(),
            }

        closed_df = today_df[today_df["status"] == "CLOSED"]
        open_df = today_df[today_df["status"] == "OPEN"]

        realized_pnl = float(pd.to_numeric(closed_df["realized_pnl"], errors="coerce").fillna(0.0).sum())
        gross_realized_pnl = float(pd.to_numeric(closed_df["gross_pnl"], errors="coerce").fillna(0.0).sum())
        total_charges = float(pd.to_numeric(closed_df["charges"], errors="coerce").fillna(0.0).sum())
        unrealized_pnl = 0.0
        open_symbols: set[str] = set()

        for _, row in open_df.iterrows():
            sym = str(row["symbol"])
            tsym = str(row["tradingsymbol"])
            open_symbols.add(sym)
            ltp = ltp_map.get(sym) or ltp_map.get(tsym)
            if ltp is not None and ltp > 0:
                qty = int(row["qty"])
                entry_px = float(row["entry_price"])
                seg = str(row["segment"] or "EQ_INTRADAY")
                est_chg = calculate_nse_charges(seg, entry_px * qty, float(ltp) * qty)["total"]  # type: ignore[arg-type]
                unrealized_pnl += ((float(ltp) - entry_px) * qty) - est_chg

        realized_pnl = round(realized_pnl, 2)
        gross_realized_pnl = round(gross_realized_pnl, 2)
        total_charges = round(total_charges, 2)
        unrealized_pnl = round(unrealized_pnl, 2)
        return {
            "realized_pnl": realized_pnl,
            "gross_realized_pnl": gross_realized_pnl,
            "total_charges": total_charges,
            "unrealized_pnl": unrealized_pnl,
            "net_pnl": round(realized_pnl + unrealized_pnl, 2),
            "trades_used": int(len(today_df)),
            "open_positions_count": int(len(open_df)),
            "open_symbols": open_symbols,
        }
