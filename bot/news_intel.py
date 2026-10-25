"""Best-effort public news and token-specific DEX-market intelligence.

Scores are transparent keyword heuristics, not a validated predictive model. DEX
metrics are an on-chain-market proxy; they are not a full audit of token contracts
or chain activity. Missing sources never count as positive confirmation.
"""
from __future__ import annotations
import json, re, time, urllib.parse, urllib.request, xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional


class NewsIntel:
    FEEDS = [
        ('CoinDesk', 'https://www.coindesk.com/arc/outboundfeeds/rss/'),
        ('Cointelegraph', 'https://cointelegraph.com/rss'),
        ('The Block', 'https://www.theblock.co/rss.xml'),
        ('Decrypt', 'https://decrypt.co/feed'),
    ]
    POSITIVE = ('approval', 'approved', 'adoption', 'inflow', 'launches', 'partnership', 'surge', 'record high', 'bullish', 'upgrade', 'etf inflow')
    NEGATIVE = ('hack', 'exploit', 'lawsuit', 'charged', 'ban', 'banned', 'outflow', 'liquidation', 'bankruptcy', 'breach', 'bearish', 'delisted', 'scam', 'rug pull')

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout
        self.last_update = 0.0
        self.news: List[Dict[str, Any]] = []
        self.chain: Dict[str, Any] = {}
        self.status = 'Waiting for first intelligence scan'
        self.errors: List[str] = []
        self._dex_cache: Dict[str, Dict[str, Any]] = {}

    def _get(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={'User-Agent': 'AutonomousAITrader/1.0 (public market data)'})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.read(1_000_000)

    @staticmethod
    def _score_title(title: str) -> int:
        low = title.lower()
        score = sum(1 for word in NewsIntel.POSITIVE if word in low) - sum(1 for word in NewsIntel.NEGATIVE if word in low)
        return max(-3, min(3, score))

    def refresh(self) -> Dict[str, Any]:
        items, errors = [], []
        for source, url in self.FEEDS:
            try:
                root = ET.fromstring(self._get(url))
                nodes = root.findall('.//item') or root.findall('.//{http://www.w3.org/2005/Atom}entry')
                for node in nodes[:20]:
                    title = (node.findtext('title') or node.findtext('{http://www.w3.org/2005/Atom}title') or '').strip()
                    link = (node.findtext('link') or '').strip()
                    if not link:
                        link_node = node.find('{http://www.w3.org/2005/Atom}link')
                        if link_node is not None:
                            link = link_node.attrib.get('href', '')
                    pub = (node.findtext('pubDate') or node.findtext('published') or node.findtext('{http://www.w3.org/2005/Atom}updated') or '').strip()
                    if title and link:
                        items.append({'source': source, 'title': title[:240], 'url': link,
                                      'published': pub[:100], 'heuristic_sentiment': self._score_title(title)})
            except Exception as e:
                errors.append(source + ': ' + type(e).__name__)
        # Bitcoin network metrics are network-level context, not token-specific evidence.
        chain = {}
        for key, url in [('hash_rate', 'https://api.blockchain.info/charts/hash-rate?timespan=1week&format=json'),
                         ('mempool_size', 'https://api.blockchain.info/charts/mempool-size?timespan=1week&format=json')]:
            try:
                data = json.loads(self._get(url).decode())
                values = data.get('values') or []
                if values:
                    chain[key] = {'latest': values[-1].get('y'), 'previous': values[-2].get('y') if len(values) > 1 else None,
                                  'timestamp': values[-1].get('x'), 'unit': data.get('unit')}
            except Exception as e:
                errors.append(key + ': ' + type(e).__name__)
        seen, unique = set(), []
        for x in items:
            k = (x['source'], x['title'].lower())
            if k not in seen:
                seen.add(k); unique.append(x)
        unique.sort(key=lambda x: x.get('published', ''), reverse=True)
        self.news, self.chain, self.last_update, self.errors = unique[:60], chain, time.time(), errors
        self.status = 'Updated' if unique or chain else ('Unavailable: ' + ', '.join(errors[:5]))
        return self.snapshot()

    def token_market(self, symbol: str) -> Dict[str, Any]:
        """Fetch/cache DEX Screener pools matching a token symbol; reject symbol-only false matches."""
        base = re.sub(r'USDT$', '', str(symbol).upper())
        now = time.time()
        cached = self._dex_cache.get(base)
        if cached and now - cached.get('fetched_at', 0) < 900:
            return cached
        result: Dict[str, Any] = {'symbol': base, 'status': 'unavailable', 'source': 'DexScreener public search',
                                  'note': 'DEX liquidity/volume and transaction counts are a market/on-chain proxy; ticker matches can be ambiguous.'}
        if not base or not re.fullmatch(r'[A-Z0-9]{2,20}', base):
            result['note'] = 'Token symbol not suitable for public DEX lookup.'
            self._dex_cache[base] = dict(result, fetched_at=now)
            return result
        try:
            url = 'https://api.dexscreener.com/latest/dex/search/?q=' + urllib.parse.quote(base)
            payload = json.loads(self._get(url).decode())
            matches = []
            for pair in payload.get('pairs') or []:
                token = pair.get('baseToken') or {}
                quote = pair.get('quoteToken') or {}
                if str(token.get('symbol', '')).upper() != base:
                    continue
                liquidity = (pair.get('liquidity') or {}).get('usd') or 0
                txns = pair.get('txns') or {}
                h1 = txns.get('h1') or {}
                h24 = txns.get('h24') or {}
                vol = pair.get('volume') or {}
                matches.append({'chain': pair.get('chainId'), 'dex': pair.get('dexId'),
                    'pair_address': pair.get('pairAddress'), 'base_name': token.get('name'),
                    'quote_symbol': quote.get('symbol'), 'price_usd': pair.get('priceUsd'),
                    'liquidity_usd': float(liquidity or 0), 'volume_24h_usd': float(vol.get('h24') or 0),
                    'transactions_1h': {'buys': int(h1.get('buys') or 0), 'sells': int(h1.get('sells') or 0)},
                    'transactions_24h': {'buys': int(h24.get('buys') or 0), 'sells': int(h24.get('sells') or 0)},
                    'price_change_24h_pct': (pair.get('priceChange') or {}).get('h24'),
                    'fdv_usd': pair.get('fdv'), 'url': pair.get('url')})
            matches.sort(key=lambda x: x['liquidity_usd'], reverse=True)
            if matches:
                best = matches[0]
                # Transparent heuristic only: liquidity and recent buy/sell activity, not a trading signal.
                buys, sells = best['transactions_1h']['buys'], best['transactions_1h']['sells']
                total = buys + sells
                result.update({'status': 'matched', 'best_pool': best, 'matching_pools_found': len(matches),
                               'heuristic_flow_bias_1h': round((buys - sells) / total, 3) if total else None,
                               'match_warning': 'Symbol-only match; verify contract and chain before treating this as the same asset.'})
            else:
                result.update({'status': 'no_match', 'note': 'No exact base-token symbol match found in public DEX search; no token-specific metrics available.'})
        except Exception as e:
            result.update({'status': 'unavailable', 'error': type(e).__name__,
                           'note': 'Public DEX endpoint failed; missing data is not confirmation.'})
        result['fetched_at'] = now
        self._dex_cache[base] = result
        return result

    def snapshot(self, symbol: Optional[str] = None, include_dex: bool = False) -> Dict[str, Any]:
        scored = [x.get('heuristic_sentiment', 0) for x in self.news[:30]]
        news_context: Dict[str, Any] = {
            'headline_count': len(self.news),
            'heuristic_sentiment_score': round(sum(scored) / len(scored), 2) if scored else None,
            'sentiment_method': 'Unvalidated keyword heuristic from headlines; not a predictive or calibrated score.',
            'recent_headlines': self.news[:12],
        }
        token_news = None
        if symbol:
            base = re.sub(r'USDT$', '', str(symbol).upper())
            matched = [x for x in self.news if re.search(r'(?<![A-Z0-9])' + re.escape(base) + r'(?![A-Z0-9])', x.get('title', '').upper())]
            token_news = {'symbol': base, 'matching_headlines': matched[:10],
                          'headline_count': len(matched),
                          'heuristic_sentiment_score': round(sum(x.get('heuristic_sentiment', 0) for x in matched) / len(matched), 2) if matched else None}
        out = {'last_update': self.last_update, 'status': self.status, 'news': self.news[:12],
               'news_analysis': news_context, 'token_news': token_news, 'bitcoin_network': self.chain,
               'source_errors': self.errors,
               'note': 'Headline scores are unvalidated keyword heuristics. DEX pool metrics are only a token-specific market/on-chain proxy; contract identity is not guaranteed by ticker symbol. External data is context only and never independently authorises a trade.'}
        if symbol:
            base = re.sub(r'USDT$', '', str(symbol).upper())
            cached = self._dex_cache.get(base)
            if include_dex:
                out['token_dex_market'] = self.token_market(symbol)
            elif cached:
                out['token_dex_market'] = cached
            else:
                out['token_dex_market'] = {'symbol': base, 'status': 'not_fetched',
                    'note': 'Token DEX metrics are fetched during an AI analysis cycle, not during dashboard polling.'}
        return out
