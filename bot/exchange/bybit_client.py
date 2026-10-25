"""Bybit V5 REST client (linear USDT perpetuals) using only the standard library.

* Public endpoints need no credentials (used by PAPER mode for live market data).
* Private endpoints are signed with HMAC-SHA256. Credentials are only held in memory,
  never logged, never put in prompts.
* The AI layer has no access to this module.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..logging_config import get_logger, register_secret
from ..risk.position_sizing import InstrumentInfo
from ..strategy.types import Candle
from .base import CODE_LEVERAGE_UNCHANGED, CODE_RATE_LIMIT, Exchange, ExchangeError

log = get_logger("bot.exchange")

MAINNET = "https://api.bybit.com"
TESTNET = "https://api-testnet.bybit.com"
RECV_WINDOW = "5000"
INTERVALS = {"1", "3", "5", "15", "30", "60", "120", "240", "360", "720", "D", "W", "M"}


def fmt(x: Any) -> str:
    """Plain decimal string (never scientific notation) for order parameters."""
    return format(Decimal(str(x)).normalize(), "f")


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v not in ("", None) else default
    except (TypeError, ValueError):
        return default


def _default_http(method: str, url: str, headers: Dict[str, str], body: Optional[str], timeout: float) -> Tuple[int, str]:
    req = urllib.request.Request(url, data=body.encode() if body else None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


class BybitExchange(Exchange):
    name = "bybit"
    simulated = False

    def __init__(self, testnet: bool = False, credentials: Optional[Tuple[str, str]] = None,
                 category: str = "linear", timeout: float = 15.0, max_retries: int = 3,
                 http: Optional[Callable[..., Tuple[int, str]]] = None,
                 sleep: Callable[[float], None] = time.sleep, base_url: Optional[str] = None):
        self.testnet = testnet
        self.base = base_url or (TESTNET if testnet else MAINNET)
        self.category = category
        self.timeout, self.max_retries = timeout, max_retries
        self._http = http or _default_http
        self._sleep = sleep
        self._key, self._secret = credentials if credentials else (None, None)
        register_secret(self._key)
        register_secret(self._secret)
        self._time_offset_ms = 0
        self._inst_cache: Dict[str, InstrumentInfo] = {}
        self.last_ok_ts: float = 0.0
        self.consecutive_failures = 0

    @property
    def has_credentials(self) -> bool:
        return bool(self._key and self._secret)

    @property
    def environment(self) -> str:
        return "TESTNET" if self.testnet else "MAINNET"

    # --- signing / transport ---------------------------------------------------------------
    def _sign(self, ts: str, payload: str) -> str:
        return hmac.new(self._secret.encode(), (ts + self._key + RECV_WINDOW + payload).encode(),
                        hashlib.sha256).hexdigest()

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, private: bool = False,
                 retry: bool = True) -> Dict[str, Any]:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if private and not self.has_credentials:
            raise ExchangeError("API credentials are required for this call", None, False)
        delay = 1.0
        last_err: Optional[ExchangeError] = None
        attempts = self.max_retries if retry else 1
        for attempt in range(attempts):
            headers = {"Content-Type": "application/json"}
            url, body = self.base + path, None
            if method == "GET":
                qs = urllib.parse.urlencode(params)
                if qs:
                    url += "?" + qs
                payload = qs
            else:
                body = json.dumps(params, separators=(",", ":"))
                payload = body
            if private:
                ts = str(int(time.time() * 1000) + self._time_offset_ms)
                headers.update({"X-BAPI-API-KEY": self._key, "X-BAPI-TIMESTAMP": ts,
                                "X-BAPI-RECV-WINDOW": RECV_WINDOW, "X-BAPI-SIGN": self._sign(ts, payload)})
            try:
                status, text = self._http(method, url, headers, body, self.timeout)
                data = json.loads(text) if text else {}
            except (OSError, ValueError, TimeoutError) as e:
                last_err = ExchangeError(f"network error calling {path}: {type(e).__name__}", None, True)
            else:
                code = data.get("retCode")
                if status == 200 and code == 0:
                    self.consecutive_failures = 0
                    self.last_ok_ts = time.time()
                    return data.get("result", {}) or {}
                retryable = status in (429, 500, 502, 503, 504) or code in CODE_RATE_LIMIT
                if code == 10002 and attempt == 0:      # timestamp skew: resync once and retry
                    try:
                        self.sync_time()
                        retryable = True
                    except ExchangeError:
                        pass
                last_err = ExchangeError(f"Bybit {path}: [{code}] {data.get('retMsg', text[:200])}", code, retryable)
                if not retryable:
                    self.consecutive_failures += 1 if status >= 500 else 0
                    raise last_err
            if attempt < attempts - 1:
                log.warning("retrying %s in %.0fs (%s)", path, delay, last_err)
                self._sleep(delay)
                delay = min(delay * 2, 20)
        self.consecutive_failures += 1
        assert last_err is not None
        raise last_err

    def sync_time(self) -> None:
        r = self._request("GET", "/v5/market/time", retry=False)
        server_ms = int(_f(r.get("timeNano", 0)) / 1e6) or int(_f(r.get("timeSecond", 0)) * 1000)
        if server_ms:
            self._time_offset_ms = server_ms - int(time.time() * 1000)

    # --- market data -------------------------------------------------------------------------------
    def get_instrument(self, symbol: str) -> InstrumentInfo:
        if symbol in self._inst_cache:
            return self._inst_cache[symbol]
        r = self._request("GET", "/v5/market/instruments-info", {"category": self.category, "symbol": symbol})
        items = r.get("list") or []
        if not items:
            raise ExchangeError(f"instrument {symbol} not found")
        i = items[0]
        lot, pf = i.get("lotSizeFilter", {}), i.get("priceFilter", {})
        info = InstrumentInfo(symbol, _f(pf.get("tickSize"), 0.1), _f(lot.get("qtyStep"), 0.001),
                              _f(lot.get("minOrderQty"), 0.001), _f(lot.get("maxOrderQty"), 1e9),
                              _f(lot.get("minNotionalValue"), 5.0))
        self._inst_cache[symbol] = info
        return info

    def get_all_tickers(self) -> List[Dict[str, Any]]:
        """Fetch one public ticker snapshot for the whole linear market."""
        r = self._request("GET", "/v5/market/tickers", {"category": self.category})
        out = []
        for t in r.get("list") or []:
            symbol = str(t.get("symbol", ""))
            if not symbol.endswith("USDT") or not symbol[:-4]:
                continue
            price, bid, ask = _f(t.get("lastPrice")), _f(t.get("bid1Price")), _f(t.get("ask1Price"))
            if price <= 0 or bid <= 0 or ask <= 0 or ask < bid:
                continue
            out.append({"symbol": symbol, "price": price, "bid": bid, "ask": ask,
                        "funding_rate": _f(t.get("fundingRate")), "volume_24h": _f(t.get("volume24h")),
                        "turnover_24h": _f(t.get("turnover24h")), "open_interest": _f(t.get("openInterest")),
                        "spread_pct": ((ask-bid)/((ask+bid)/2))*100})
        return out

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        r = self._request("GET", "/v5/market/tickers", {"category": self.category, "symbol": symbol})
        items = r.get("list") or []
        if not items:
            raise ExchangeError(f"no ticker for {symbol}")
        t = items[0]
        return {"symbol": symbol, "price": _f(t.get("lastPrice")), "bid": _f(t.get("bid1Price")),
                "ask": _f(t.get("ask1Price")), "mark": _f(t.get("markPrice")), "index": _f(t.get("indexPrice")),
                "funding_rate": _f(t.get("fundingRate")), "open_interest": _f(t.get("openInterest")),
                "volume_24h": _f(t.get("volume24h")), "ts": time.time()}

    def get_klines(self, symbol: str, interval: str, limit: int = 200, start_ms: Optional[int] = None,
                   end_ms: Optional[int] = None) -> List[Candle]:
        if interval not in INTERVALS:
            raise ExchangeError(f"unsupported interval {interval}")
        r = self._request("GET", "/v5/market/kline", {"category": self.category, "symbol": symbol,
                                                      "interval": interval, "limit": min(limit, 1000),
                                                      "start": start_ms, "end": end_ms})
        rows = r.get("list") or []
        candles = [Candle(int(x[0]), _f(x[1]), _f(x[2]), _f(x[3]), _f(x[4]), _f(x[5])) for x in rows]
        candles.sort(key=lambda c: c.ts)      # Bybit returns newest first
        return candles

    def get_orderbook(self, symbol: str, limit: int = 25) -> Dict[str, Any]:
        r = self._request("GET", "/v5/market/orderbook", {"category": self.category, "symbol": symbol, "limit": limit})
        return {"bids": [[_f(p), _f(q)] for p, q in r.get("b", [])], "asks": [[_f(p), _f(q)] for p, q in r.get("a", [])]}

    # --- account ----------------------------------------------------------------------------------------
    def get_wallet(self) -> Dict[str, float]:
        r = self._request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"}, private=True)
        items = r.get("list") or []
        if not items:
            raise ExchangeError("wallet balance unavailable")
        a = items[0]
        equity = _f(a.get("totalEquity"))
        avail = _f(a.get("totalAvailableBalance"))
        if equity <= 0:
            for coin in a.get("coin", []):
                if coin.get("coin") == "USDT":
                    equity = _f(coin.get("equity"))
                    avail = _f(coin.get("availableToWithdraw"), avail)
        return {"equity": equity, "available": avail}

    @staticmethod
    def _norm_position(p: Dict[str, Any]) -> Dict[str, Any]:
        return {"symbol": p.get("symbol"), "side": p.get("side", ""), "size": _f(p.get("size")),
                "avgPrice": _f(p.get("avgPrice")), "stopLoss": _f(p.get("stopLoss")),
                "takeProfit": _f(p.get("takeProfit")), "unrealisedPnl": _f(p.get("unrealisedPnl")),
                "leverage": _f(p.get("leverage")), "markPrice": _f(p.get("markPrice")),
                "trailingStop": _f(p.get("trailingStop")), "positionIdx": int(_f(p.get("positionIdx")))}

    def get_positions(self, symbol: str) -> List[Dict[str, Any]]:
        r = self._request("GET", "/v5/position/list", {"category": self.category, "symbol": symbol}, private=True)
        return [self._norm_position(p) for p in r.get("list", []) if _f(p.get("size")) > 0]

    def get_all_positions(self) -> List[Dict[str, Any]]:
        """Read all non-zero USDT-settled linear positions with cursor pagination."""
        out, cursor = [], None
        while True:
            params = {"category": self.category, "settleCoin": "USDT", "limit": 200, "cursor": cursor}
            r = self._request("GET", "/v5/position/list", params, private=True)
            out.extend(self._norm_position(p) for p in r.get("list", []) if _f(p.get("size")) > 0)
            nxt = r.get("nextPageCursor")
            if not nxt or nxt == cursor:
                break
            cursor = nxt
        return out

    @staticmethod
    def _norm_order(o: Dict[str, Any]) -> Dict[str, Any]:
        return {"symbol": o.get("symbol"), "side": o.get("side"), "orderType": o.get("orderType"),
                "qty": _f(o.get("qty")), "price": _f(o.get("price")), "orderId": o.get("orderId"),
                "orderLinkId": o.get("orderLinkId", ""), "orderStatus": o.get("orderStatus"),
                "reduceOnly": bool(o.get("reduceOnly")), "stopLoss": _f(o.get("stopLoss")),
                "takeProfit": _f(o.get("takeProfit")), "avgPrice": _f(o.get("avgPrice")),
                "cumExecQty": _f(o.get("cumExecQty")), "cumExecFee": _f(o.get("cumExecFee"))}

    def get_open_orders(self, symbol: str) -> List[Dict[str, Any]]:
        r = self._request("GET", "/v5/order/realtime", {"category": self.category, "symbol": symbol, "openOnly": 0,
                                                       "limit": 50}, private=True)
        return [self._norm_order(o) for o in r.get("list", [])
                if o.get("orderStatus") in ("New", "PartiallyFilled", "Untriggered")]

    def get_all_open_orders(self) -> List[Dict[str, Any]]:
        """Read open USDT-settled linear orders account-wide with cursor pagination."""
        out, cursor = [], None
        while True:
            params = {"category": self.category, "settleCoin": "USDT", "openOnly": 0, "limit": 50, "cursor": cursor}
            r = self._request("GET", "/v5/order/realtime", params, private=True)
            out.extend(self._norm_order(o) for o in r.get("list", [])
                       if o.get("orderStatus") in ("New", "PartiallyFilled", "Untriggered"))
            nxt = r.get("nextPageCursor")
            if not nxt or nxt == cursor:
                break
            cursor = nxt
        return out

    def get_order(self, symbol: str, order_link_id: str) -> Optional[Dict[str, Any]]:
        for path in ("/v5/order/realtime", "/v5/order/history"):
            r = self._request("GET", path, {"category": self.category, "symbol": symbol,
                                            "orderLinkId": order_link_id}, private=True)
            items = r.get("list") or []
            if items:
                return self._norm_order(items[0])
        return None

    # --- trading --------------------------------------------------------------------------------------------
    def place_order(self, **p: Any) -> Dict[str, Any]:
        body: Dict[str, Any] = {"category": self.category, "symbol": p["symbol"], "side": p["side"],
                                "orderType": p["orderType"], "qty": fmt(p["qty"]), "positionIdx": 0,
                                "orderLinkId": p["orderLinkId"]}
        if p["orderType"] == "Limit":
            body.update(price=fmt(p["price"]), timeInForce=p.get("timeInForce", "GTC"))
        else:
            body["timeInForce"] = "IOC"
        if p.get("reduceOnly"):
            body["reduceOnly"] = True
        if p.get("stopLoss") and not p.get("reduceOnly"):
            body.update(stopLoss=fmt(p["stopLoss"]), slTriggerBy="MarkPrice", tpslMode="Full")
        if p.get("takeProfit") and not p.get("reduceOnly"):
            body.update(takeProfit=fmt(p["takeProfit"]), tpTriggerBy="MarkPrice", tpslMode="Full")
        # orderLinkId makes a retry after a network error idempotent (duplicate => code 110072)
        r = self._request("POST", "/v5/order/create", body, private=True)
        return {"orderId": r.get("orderId"), "orderLinkId": r.get("orderLinkId", p["orderLinkId"])}

    def cancel_order(self, symbol: str, order_link_id: str) -> None:
        self._request("POST", "/v5/order/cancel", {"category": self.category, "symbol": symbol,
                                                   "orderLinkId": order_link_id}, private=True)

    def cancel_all_orders(self, symbol: str) -> None:
        self._request("POST", "/v5/order/cancel-all", {"category": self.category, "symbol": symbol}, private=True)

    def set_leverage(self, symbol: str, leverage: int) -> None:
        try:
            self._request("POST", "/v5/position/set-leverage", {"category": self.category, "symbol": symbol,
                                                                "buyLeverage": str(int(leverage)),
                                                                "sellLeverage": str(int(leverage))}, private=True)
        except ExchangeError as e:
            if e.code != CODE_LEVERAGE_UNCHANGED:
                raise

    def set_trading_stop(self, symbol: str, stop_loss: Optional[float] = None, take_profit: Optional[float] = None) -> None:
        body: Dict[str, Any] = {"category": self.category, "symbol": symbol, "positionIdx": 0, "tpslMode": "Full"}
        if stop_loss is not None:
            body.update(stopLoss=fmt(stop_loss), slTriggerBy="MarkPrice")
        if take_profit is not None:
            body.update(takeProfit=fmt(take_profit), tpTriggerBy="MarkPrice")
        self._request("POST", "/v5/position/trading-stop", body, private=True)

    def get_executions(self, symbol: str, limit: int = 50) -> List[Dict[str, Any]]:
        r = self._request("GET", "/v5/execution/list", {"category": self.category, "symbol": symbol, "limit": limit}, private=True)
        return [{"execId": e.get("execId"), "orderLinkId": e.get("orderLinkId", ""), "symbol": e.get("symbol"),
                 "side": e.get("side"), "execPrice": _f(e.get("execPrice")), "execQty": _f(e.get("execQty")),
                 "execFee": _f(e.get("execFee")), "execTime": _f(e.get("execTime")) / 1000} for e in r.get("list", [])]

    def get_closed_pnl(self, symbol: str, limit: int = 20) -> List[Dict[str, Any]]:
        r = self._request("GET", "/v5/position/closed-pnl", {"category": self.category, "symbol": symbol, "limit": limit}, private=True)
        out = []
        for c in r.get("list", []):
            out.append({"symbol": c.get("symbol"), "side": c.get("side"), "entry": _f(c.get("avgEntryPrice")),
                        "exit": _f(c.get("avgExitPrice")), "qty": _f(c.get("qty")), "pnl": _f(c.get("closedPnl")),
                        "fees": _f(c.get("openFee")) + _f(c.get("closeFee")), "reason": "exchange",
                        "closeLink": c.get("orderId", ""), "time": _f(c.get("updatedTime")) / 1000})
        return out

    # --- permissions ---------------------------------------------------------------------------------------------
    def check_api_permissions(self) -> Dict[str, Any]:
        """Refuse any key that can withdraw. Returns {ok, withdraw, read_only, ips, problems[]}."""
        r = self._request("GET", "/v5/user/query-api", private=True)
        perms = r.get("permissions", {}) or {}
        flat = json.dumps(perms).lower()
        withdraw = "withdraw" in flat
        read_only = bool(int(_f(r.get("readOnly"))))
        trade = bool(perms.get("ContractTrade") or perms.get("Derivatives"))
        problems = []
        if withdraw:
            problems.append("API key HAS WITHDRAWAL permission - create a new key without it")
        if read_only:
            problems.append("API key is read-only - it cannot place orders")
        if not trade and not read_only:
            problems.append("API key has no Contract/Derivatives trade permission")
        ips = r.get("ips") or []
        return {"ok": not withdraw and not read_only and trade, "withdraw": withdraw, "read_only": read_only,
                "ip_restricted": bool(ips) and ips != ["*"], "problems": problems,
                "expires": r.get("expiredAt")}
