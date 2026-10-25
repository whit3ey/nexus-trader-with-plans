import time
import unittest
from bot.execution.dedupe import make_order_link_id, check_duplicate

LID = make_order_link_id("BTCUSDT", "LONG", 1_790_000_000_000)


class DedupeTests(unittest.TestCase):
    def test_id_is_deterministic_and_bybit_safe(self):
        self.assertEqual(LID, make_order_link_id("BTCUSDT", "LONG", 1_790_000_000_000))
        self.assertLessEqual(len(LID), 36)
        self.assertNotEqual(LID, make_order_link_id("BTCUSDT", "LONG", 1_790_000_300_000))
        self.assertNotEqual(LID, make_order_link_id("BTCUSDT", "SHORT", 1_790_000_000_000))
        self.assertNotEqual(LID, make_order_link_id("BTCUSDT", "LONG", 1_790_000_000_000, purpose="sl"))

    def test_clean_state_allows_order(self):
        self.assertFalse(check_duplicate(LID, "BTCUSDT", "LONG", [], [], []).duplicate)

    def test_existing_position_blocks(self):
        r = check_duplicate(LID, "BTCUSDT", "LONG", [{"symbol": "BTCUSDT", "size": "0.01", "side": "Buy"}], [], [])
        self.assertTrue(r.duplicate)
        self.assertFalse(check_duplicate(LID, "BTCUSDT", "LONG", [{"symbol": "BTCUSDT", "size": "0", "side": ""}], [], []).duplicate)

    def test_open_order_same_link_or_equivalent_blocks(self):
        same = [{"symbol": "BTCUSDT", "side": "Sell", "orderLinkId": LID}]
        self.assertTrue(check_duplicate(LID, "BTCUSDT", "LONG", [], same, []).duplicate)
        equiv = [{"symbol": "BTCUSDT", "side": "Buy", "orderLinkId": "other", "reduceOnly": False}]
        self.assertTrue(check_duplicate(LID, "BTCUSDT", "LONG", [], equiv, []).duplicate)

    def test_reduce_only_order_does_not_count_as_opening(self):
        ro = [{"symbol": "BTCUSDT", "side": "Buy", "orderLinkId": "x", "reduceOnly": True}]
        self.assertFalse(check_duplicate(LID, "BTCUSDT", "LONG", [], ro, []).duplicate)

    def test_local_record_with_same_link_id_blocks(self):
        local = [{"order_link_id": LID, "symbol": "BTCUSDT", "direction": "LONG", "created_ts": time.time() - 5000, "status": "filled"}]
        self.assertTrue(check_duplicate(LID, "BTCUSDT", "LONG", [], [], local).duplicate)

    def test_recent_equivalent_local_order_blocks_but_old_or_failed_do_not(self):
        now = time.time()
        def rec(age, status="new"):
            return [{"order_link_id": "z", "symbol": "BTCUSDT", "direction": "LONG", "purpose": "open",
                     "created_ts": now - age, "status": status}]
        self.assertTrue(check_duplicate(LID, "BTCUSDT", "LONG", [], [], rec(60), now=now).duplicate)
        self.assertFalse(check_duplicate(LID, "BTCUSDT", "LONG", [], [], rec(5000), now=now).duplicate)
        self.assertFalse(check_duplicate(LID, "BTCUSDT", "LONG", [], [], rec(60, "rejected"), now=now).duplicate)
        self.assertFalse(check_duplicate(LID, "BTCUSDT", "SHORT", [], [], rec(60), now=now).duplicate)


if __name__ == "__main__":
    unittest.main()
