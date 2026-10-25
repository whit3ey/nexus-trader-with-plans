import unittest
from unittest import mock
from bot.news_intel import NewsIntel

class NewsIntelTests(unittest.TestCase):
    def test_headline_score_is_bounded_and_transparent(self):
        self.assertGreater(NewsIntel._score_title('ETF approval and adoption surge'), 0)
        self.assertLess(NewsIntel._score_title('Major exploit and ban announced'), 0)
        self.assertEqual(NewsIntel._score_title('Bitcoin price moves today'), 0)
        self.assertLessEqual(NewsIntel._score_title('approval adoption surge partnership upgrade'), 3)

    def test_token_market_exact_symbol_match_and_liquidity_rank(self):
        intel = NewsIntel()
        payload = {'pairs': [
            {'baseToken': {'symbol': 'ABC', 'name': 'ABC Token'}, 'quoteToken': {'symbol': 'USDT'},
             'chainId': 'ethereum', 'dexId': 'dex-a', 'pairAddress': 'low', 'liquidity': {'usd': 100},
             'volume': {'h24': 200}, 'txns': {'h1': {'buys': 3, 'sells': 1}, 'h24': {}}, 'priceChange': {'h24': 2}, 'url': 'https://example.com/low'},
            {'baseToken': {'symbol': 'ABC', 'name': 'ABC Token'}, 'quoteToken': {'symbol': 'USDC'},
             'chainId': 'base', 'dexId': 'dex-b', 'pairAddress': 'high', 'liquidity': {'usd': 500},
             'volume': {'h24': 900}, 'txns': {'h1': {'buys': 2, 'sells': 2}, 'h24': {}}, 'priceChange': {'h24': -1}, 'url': 'https://example.com/high'},
            {'baseToken': {'symbol': 'ABCD', 'name': 'Wrong ticker'}, 'quoteToken': {'symbol': 'USDT'},
             'chainId': 'ethereum', 'dexId': 'dex-c', 'liquidity': {'usd': 99999}},
        ]}
        with mock.patch.object(intel, '_get', return_value=__import__('json').dumps(payload).encode()):
            result = intel.token_market('ABCUSDT')
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['best_pool']['pair_address'], 'high')
        self.assertEqual(result['matching_pools_found'], 2)
        self.assertEqual(result['heuristic_flow_bias_1h'], 0.0)

    def test_snapshot_marks_sentiment_as_unvalidated(self):
        intel = NewsIntel()
        intel.news = [{'title': 'ABC approval', 'source': 'test', 'url': 'https://example.com', 'published': '', 'heuristic_sentiment': 1}]
        snap = intel.snapshot('ABCUSDT', include_dex=True)
        self.assertIn('unvalidated', snap['news_analysis']['sentiment_method'].lower())
        self.assertEqual(snap['token_news']['headline_count'], 1)

if __name__ == '__main__':
    unittest.main()
