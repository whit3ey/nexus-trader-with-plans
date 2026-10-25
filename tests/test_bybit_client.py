import hashlib
import hmac
import json
import unittest
import urllib.parse

from bot.exchange.base import ExchangeError
from bot.exchange.bybit_client import BybitExchange, fmt, TESTNET, MAINNET

KEY, SECRET = "TESTKEY12345", "TESTSECRET67890"


class FakeServer:
    def __init__(self):
        self.calls, self.routes = [], {}

    def on(self, path, *replies):
        self.routes[path] = list(replies)

    def __call__(self, method, url, headers, body, timeout):
        p = urllib.parse.urlparse(url)
        self.calls.append({"method": method, "path": p.path, "query": p.query, "headers": headers, "body": body})
        replies = self.routes.get(p.path)
        if not replies:
            return 404, "{}"
        r = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(r, Exception):
            raise r
        if isinstance(r, tuple):
            return r
        return 200, json.dumps({"retCode": 0, "retMsg": "OK", "result": r})


def ok(result):
    return result


def make(srv, creds=(KEY, SECRET), testnet=True, **kw):
    sleeps = []
    ex = BybitExchange(testnet=testnet, credentials=creds, http=srv, sleep=sleeps.append, **kw)
    ex.sleeps = sleeps
    return ex


class SigningTests(unittest.TestCase):
    def test_get_request_is_signed_over_the_query_string(self):
        srv = FakeServer()
        srv.on("/v5/position/list", {"list": []})
        make(srv).get_positions("BTCUSDT")
        c = srv.calls[0]
        h = c["headers"]
        expected = hmac.new(SECRET.encode(), (h["X-BAPI-TIMESTAMP"] + KEY + "5000" + c["query"]).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(h["X-BAPI-SIGN"], expected)
        self.assertEqual(h["X-BAPI-API-KEY"], KEY)
        self.assertEqual(h["X-BAPI-RECV-WINDOW"], "5000")
        self.assertIn("category=linear", c["query"])

    def test_post_request_is_signed_over_the_json_body(self):
        srv = FakeServer()
        srv.on("/v5/order/cancel", {"orderId": "1"})
        make(srv).cancel_order("BTCUSDT", "lid")
        c = srv.calls[0]
        h = c["headers"]
        expected = hmac.new(SECRET.encode(), (h["X-BAPI-TIMESTAMP"] + KEY + "5000" + c["body"]).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(h["X-BAPI-SIGN"], expected)
        self.assertEqual(json.loads(c["body"])["orderLinkId"], "lid")

    def test_public_calls_send_no_credentials(self):
        srv = FakeServer()
        srv.on("/v5/market/tickers", {"list": [{"lastPrice": "100", "bid1Price": "99", "ask1Price": "101"}]})
        make(srv, creds=None).get_ticker("BTCUSDT")
        self.assertNotIn("X-BAPI-API-KEY", srv.calls[0]["headers"])
        self.assertNotIn("X-BAPI-SIGN", srv.calls[0]["headers"])

    def test_private_call_without_credentials_fails_before_any_request(self):
        srv = FakeServer()
        with self.assertRaises(ExchangeError):
            make(srv, creds=None).get_wallet()
        self.assertEqual(srv.calls, [])

    def test_environment_selects_the_matching_endpoint(self):
        self.assertEqual(make(FakeServer(), testnet=True).base, TESTNET)
        self.assertEqual(make(FakeServer(), testnet=False).base, MAINNET)
        self.assertEqual(make(FakeServer(), testnet=True).environment, "TESTNET")


class ParsingTests(unittest.TestCase):
    def test_klines_sorted_oldest_first(self):
        srv = FakeServer()
        srv.on("/v5/market/kline", {"list": [["3000", "3", "4", "2", "3.5", "10", "0"], ["1000", "1", "2", "0.5", "1.5", "5", "0"]]})
        cs = make(srv, creds=None).get_klines("BTCUSDT", "5", 2)
        self.assertEqual([c.ts for c in cs], [1000, 3000])
        self.assertEqual((cs[1].open, cs[1].high, cs[1].low, cs[1].close, cs[1].volume), (3.0, 4.0, 2.0, 3.5, 10.0))

    def test_unsupported_interval_rejected(self):
        with self.assertRaises(ExchangeError):
            make(FakeServer(), creds=None).get_klines("BTCUSDT", "7", 10)

    def test_ticker_fields(self):
        srv = FakeServer()
        srv.on("/v5/market/tickers", {"list": [{"lastPrice": "100000.5", "bid1Price": "100000", "ask1Price": "100001",
                                                "markPrice": "100000.2", "indexPrice": "100000.1", "fundingRate": "0.0001",
                                                "openInterest": "5000"}]})
        t = make(srv, creds=None).get_ticker("BTCUSDT")
        self.assertEqual((t["price"], t["bid"], t["ask"], t["mark"], t["funding_rate"], t["open_interest"]),
                         (100000.5, 100000.0, 100001.0, 100000.2, 0.0001, 5000.0))

    def test_instrument_rules_cached(self):
        srv = FakeServer()
        srv.on("/v5/market/instruments-info", {"list": [{"lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001",
                                                                           "maxOrderQty": "100", "minNotionalValue": "5"},
                                                         "priceFilter": {"tickSize": "0.10"}}]})
        ex = make(srv, creds=None)
        i = ex.get_instrument("BTCUSDT")
        ex.get_instrument("BTCUSDT")
        self.assertEqual((i.tick_size, i.qty_step, i.min_qty, i.max_qty, i.min_notional), (0.1, 0.001, 0.001, 100.0, 5.0))
        self.assertEqual(len(srv.calls), 1)

    def test_wallet(self):
        srv = FakeServer()
        srv.on("/v5/account/wallet-balance", {"list": [{"totalEquity": "10250.5", "totalAvailableBalance": "9800.1"}]})
        self.assertEqual(make(srv).get_wallet(), {"equity": 10250.5, "available": 9800.1})

    def test_positions_filter_empty_and_normalise(self):
        srv = FakeServer()
        srv.on("/v5/position/list", {"list": [
            {"symbol": "BTCUSDT", "side": "Buy", "size": "0.05", "avgPrice": "100000", "stopLoss": "99700", "takeProfit": "",
             "unrealisedPnl": "12.5", "leverage": "2", "markPrice": "100250"},
            {"symbol": "BTCUSDT", "side": "", "size": "0"}]})
        p = make(srv).get_positions("BTCUSDT")
        self.assertEqual(len(p), 1)
        self.assertEqual((p[0]["side"], p[0]["size"], p[0]["stopLoss"], p[0]["takeProfit"]), ("Buy", 0.05, 99700.0, 0.0))

    def test_open_orders_only_active(self):
        srv = FakeServer()
        srv.on("/v5/order/realtime", {"list": [{"orderStatus": "New", "orderLinkId": "a", "side": "Buy"},
                                               {"orderStatus": "Filled", "orderLinkId": "b", "side": "Buy"}]})
        o = make(srv).get_open_orders("BTCUSDT")
        self.assertEqual([x["orderLinkId"] for x in o], ["a"])

    def test_get_order_falls_back_to_history(self):
        srv = FakeServer()
        srv.on("/v5/order/realtime", {"list": []})
        srv.on("/v5/order/history", {"list": [{"orderStatus": "Filled", "orderLinkId": "z", "avgPrice": "100"}]})
        self.assertEqual(make(srv).get_order("BTCUSDT", "z")["orderStatus"], "Filled")


class OrderTests(unittest.TestCase):
    def test_market_order_body_has_sl_tp_idempotency_and_plain_decimals(self):
        srv = FakeServer()
        srv.on("/v5/order/create", {"orderId": "o1", "orderLinkId": "ait-1"})
        make(srv).place_order(symbol="BTCUSDT", side="Buy", orderType="Market", qty=0.0001, orderLinkId="ait-1",
                              stopLoss=99_700.0, takeProfit=100_700.5)
        b = json.loads(srv.calls[0]["body"])
        self.assertEqual((b["category"], b["symbol"], b["side"], b["orderType"]), ("linear", "BTCUSDT", "Buy", "Market"))
        self.assertEqual(b["qty"], "0.0001")                      # not 1e-04
        self.assertEqual((b["stopLoss"], b["takeProfit"]), ("99700", "100700.5"))
        self.assertEqual(b["orderLinkId"], "ait-1")
        self.assertEqual(b["slTriggerBy"], "MarkPrice")
        self.assertNotIn("reduceOnly", b)

    def test_limit_order_has_price_and_gtc(self):
        srv = FakeServer()
        srv.on("/v5/order/create", {"orderId": "o1"})
        make(srv).place_order(symbol="BTCUSDT", side="Sell", orderType="Limit", qty=0.01, price=100_000.5, orderLinkId="x",
                              stopLoss=100_300, takeProfit=99_300)
        b = json.loads(srv.calls[0]["body"])
        self.assertEqual((b["price"], b["timeInForce"]), ("100000.5", "GTC"))

    def test_reduce_only_order_never_carries_sl_tp(self):
        srv = FakeServer()
        srv.on("/v5/order/create", {"orderId": "o1"})
        make(srv).place_order(symbol="BTCUSDT", side="Sell", orderType="Market", qty=0.05, orderLinkId="c", reduceOnly=True,
                              stopLoss=1, takeProfit=2)
        b = json.loads(srv.calls[0]["body"])
        self.assertIs(b["reduceOnly"], True)
        self.assertNotIn("stopLoss", b)

    def test_trading_stop_and_leverage(self):
        srv = FakeServer()
        srv.on("/v5/position/trading-stop", {})
        srv.on("/v5/position/set-leverage", (200, json.dumps({"retCode": 110043, "retMsg": "leverage not modified"})))
        ex = make(srv)
        ex.set_trading_stop("BTCUSDT", stop_loss=99_800.0)
        ex.set_leverage("BTCUSDT", 3)      # "not modified" is treated as success
        b = json.loads(srv.calls[0]["body"])
        self.assertEqual((b["stopLoss"], b["positionIdx"]), ("99800", 0))
        self.assertNotIn("takeProfit", b)
        self.assertEqual(json.loads(srv.calls[1]["body"])["buyLeverage"], "3")

    def test_fmt(self):
        self.assertEqual(fmt(1e-05), "0.00001")
        self.assertEqual(fmt(100000), "100000")
        self.assertEqual(fmt(0.166), "0.166")


class ErrorHandlingTests(unittest.TestCase):
    def test_rate_limit_is_retried_with_exponential_backoff(self):
        srv = FakeServer()
        limited = (200, json.dumps({"retCode": 10006, "retMsg": "too many visits"}))
        srv.on("/v5/market/tickers", limited, limited, {"list": [{"lastPrice": "1", "bid1Price": "1", "ask1Price": "1"}]})
        ex = make(srv, creds=None)
        self.assertEqual(ex.get_ticker("BTCUSDT")["price"], 1.0)
        self.assertEqual(ex.sleeps, [1.0, 2.0])

    def test_retries_are_bounded(self):
        srv = FakeServer()
        srv.on("/v5/market/tickers", (200, json.dumps({"retCode": 10006, "retMsg": "limit"})))
        ex = make(srv, creds=None, max_retries=3)
        with self.assertRaises(ExchangeError) as cm:
            ex.get_ticker("BTCUSDT")
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(len(srv.calls), 3)
        self.assertEqual(ex.consecutive_failures, 1)

    def test_business_error_is_not_retried(self):
        srv = FakeServer()
        srv.on("/v5/order/create", (200, json.dumps({"retCode": 110007, "retMsg": "insufficient margin"})))
        ex = make(srv)
        with self.assertRaises(ExchangeError) as cm:
            ex.place_order(symbol="BTCUSDT", side="Buy", orderType="Market", qty=1, orderLinkId="a")
        self.assertEqual(cm.exception.code, 110007)
        self.assertFalse(cm.exception.retryable)
        self.assertEqual(len(srv.calls), 1)

    def test_network_error_retried_then_raised(self):
        srv = FakeServer()
        srv.on("/v5/market/tickers", OSError("dns failure"))
        ex = make(srv, creds=None, max_retries=2)
        with self.assertRaises(ExchangeError) as cm:
            ex.get_ticker("BTCUSDT")
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(len(srv.calls), 2)

    def test_http_500_is_retryable(self):
        srv = FakeServer()
        srv.on("/v5/market/tickers", (503, "<html>down</html>"), {"list": [{"lastPrice": "2", "bid1Price": "2", "ask1Price": "2"}]})
        self.assertEqual(make(srv, creds=None).get_ticker("BTCUSDT")["price"], 2.0)

    def test_duplicate_order_code_is_flagged(self):
        srv = FakeServer()
        srv.on("/v5/order/create", (200, json.dumps({"retCode": 110072, "retMsg": "OrderLinkedID is duplicate"})))
        with self.assertRaises(ExchangeError) as cm:
            make(srv).place_order(symbol="BTCUSDT", side="Buy", orderType="Market", qty=1, orderLinkId="a")
        self.assertTrue(cm.exception.is_duplicate)

    def test_secrets_never_appear_in_errors(self):
        srv = FakeServer()
        srv.on("/v5/account/wallet-balance", (200, json.dumps({"retCode": 10003, "retMsg": "API key is invalid"})))
        with self.assertRaises(ExchangeError) as cm:
            make(srv).get_wallet()
        self.assertNotIn(SECRET, str(cm.exception))
        self.assertNotIn(KEY, str(cm.exception))


class PermissionTests(unittest.TestCase):
    def check(self, result):
        srv = FakeServer()
        srv.on("/v5/user/query-api", result)
        return make(srv).check_api_permissions()

    def test_trade_only_key_is_accepted(self):
        r = self.check({"readOnly": 0, "ips": ["1.2.3.4"], "permissions": {"ContractTrade": ["Order", "Position"], "Wallet": ["AccountTransfer"]}})
        self.assertTrue(r["ok"])
        self.assertTrue(r["ip_restricted"])

    def test_withdraw_permission_is_refused(self):
        r = self.check({"readOnly": 0, "ips": ["*"], "permissions": {"ContractTrade": ["Order"], "Wallet": ["Withdraw"]}})
        self.assertFalse(r["ok"])
        self.assertTrue(r["withdraw"])
        self.assertTrue(any("WITHDRAWAL" in p for p in r["problems"]))

    def test_read_only_and_no_trade_keys_flagged(self):
        self.assertFalse(self.check({"readOnly": 1, "permissions": {"ContractTrade": ["Order"]}})["ok"])
        self.assertFalse(self.check({"readOnly": 0, "permissions": {"Wallet": ["AccountTransfer"]}})["ok"])
        self.assertFalse(self.check({"readOnly": 0, "ips": [], "permissions": {"ContractTrade": ["Order"]}})["ip_restricted"])


if __name__ == "__main__":
    unittest.main()
