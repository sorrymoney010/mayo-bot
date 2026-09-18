"""Sentiment Trading Agent — Stage 1 (free, dependency-free).

Collects text from public crypto RSS news feeds and Reddit public JSON
endpoints, scores it with a compact finance-aware lexicon (no NLTK/VADER
dependency), and produces a per-coin sentiment index in [-1, 1].

Design constraints:
* No paid APIs, no LLM calls — pure stdlib + ``requests``/``urllib``.
* Sentiment is a *confirmation filter*, never the sole entry trigger. A
  strongly bearish reading blocks a BUY; a sharp collapse can force a SELL
  exit. This prevents the bot from becoming reverse-pump exit liquidity.
* All network access is best-effort: a failed fetch degrades to a neutral
  (0.0) reading rather than blocking trading.
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ── finance-aware sentiment lexicon ───────────────────────────────
# Weight per term. Tuned for crypto/small-cap discourse. Negations flip sign.
_BULL = {
    "bullish": 2.0, "moon": 1.5, "rocket": 1.5, "pump": 1.2, "surge": 1.5,
    "rally": 1.5, "breakout": 1.3, "gain": 1.0, "gains": 1.0, "soar": 1.5,
    "buy": 0.8, "long": 0.6, "accumulate": 1.0, "uptrend": 1.2, "ath": 1.5,
    "partnership": 1.2, "listing": 1.2, "adoption": 1.2, "green": 0.6,
    "upgrade": 1.0, "optimistic": 1.0, "strong": 0.7, "win": 1.0,
}
_BEAR = {
    "bearish": -2.0, "dump": -1.5, "crash": -2.0, "plunge": -1.8, "fall": -1.0,
    "drop": -1.0, "dumps": -1.5, "sell": -0.8, "short": -0.6, "scam": -2.0,
    "rug": -2.5, "rugpull": -2.5, "ponzi": -2.0, "hack": -2.0, "exploit": -1.8,
    "delisting": -1.8, "lawsuit": -1.5, "sec": -0.8, "fud": -1.2, "red": -0.6,
    "downgrade": -1.0, "bankrupt": -2.5, "collapse": -2.0, "weak": -0.7,
    "loss": -1.0, "losses": -1.0, "ban": -1.3, "warning": -0.8,
}
_NEGATIONS = {"not", "no", "never", "isn't", "isnt", "dont", "don't", "won't",
              "wont", "cant", "can't", "without", "fail", "failed", "unlikely"}

# Coin aliases → canonical symbol base used for keyword matching.
_COIN_ALIASES = {
    "XRP": {"xrp", "ripple"}, "BTC": {"btc", "bitcoin", "xbt"},
    "TRX": {"trx", "tron"}, "DOGE": {"doge", "dogecoin"},
    "PUMP": {"pump", "pump.fun", "pumpfun"}, "KAITO": {"kaito"},
    "UNI": {"uni", "uniswap"}, "JTO": {"jto", "jito"}, "HYPE": {"hype", "hyperliquid"},
    "SOL": {"sol", "solana"}, "ADA": {"ada", "cardano"},
}


@dataclass
class SentimentIndex:
    """Per-coin sentiment result."""

    coin: str
    score: float = 0.0          # [-1, 1], 0 = neutral / unknown
    sample_size: int = 0        # number of texts scored
    source_count: int = 0       # distinct sources contributing
    fresh: bool = False         # True if fetched this cycle (vs stale/neutral)

    def blocks_buy(self, threshold: float = -0.15) -> bool:
        """Bearish sentiment blocks a fresh BUY entry."""
        return self.fresh and self.score <= threshold

    def triggers_exit(self, floor: float = -0.55) -> bool:
        """Sharp sentiment collapse forces a protective SELL exit."""
        return self.fresh and self.score <= floor


@dataclass
class SentimentConfig:
    enabled: bool = True
    # RSS/Atom news feeds (crypto-focused, public, no key required).
    news_feeds: List[str] = field(default_factory=lambda: [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
    ])
    # Public subreddits (hot posts via .json, no auth).
    subreddits: List[str] = field(default_factory=lambda: [
        "CryptoCurrency", "CryptoMarkets", "SatoshiStreetBets",
    ])
    # Per-cycle article cap per source (keep it cheap).
    max_per_source: int = 15
    # Network timeout (seconds) — never block trading on a slow fetch.
    timeout_s: float = 6.0
    # How long a cached reading stays fresh (seconds).
    cache_ttl_s: float = 900.0
    # Score below this blocks BUY; at/below this forces SELL exit.
    buy_block_threshold: float = -0.15
    exit_floor: float = -0.55


class SentimentAgent:
    """Fetches, scores, and caches per-coin sentiment."""

    def __init__(self, config: Optional[SentimentConfig] = None) -> None:
        self.cfg = config or SentimentConfig()
        self._cache: Dict[str, SentimentIndex] = {}
        self._fetched_at: float = 0.0

    # ── public API ──────────────────────────────────────────────

    def refresh(self) -> Dict[str, SentimentIndex]:
        """Fetch all sources and rebuild the per-coin index. Best-effort."""
        if not self.cfg.enabled:
            return {}
        now = time.time()
        if self._cache and (now - self._fetched_at) < self.cfg.cache_ttl_s:
            return self._cache

        texts: List[str] = []
        src_count = 0
        for feed in self.cfg.news_feeds:
            items = self._fetch_rss(feed)
            if items:
                src_count += 1
                texts.extend(items[: self.cfg.max_per_source])
        for sub in self.cfg.subreddits:
            items = self._fetch_reddit(sub)
            if items:
                src_count += 1
                texts.extend(items[: self.cfg.max_per_source])

        if not texts:
            # Network failed entirely — return neutral, but mark not-fresh so we
            # don't wrongly block/force trades on a dead feed.
            self._cache = {}
            self._fetched_at = now
            return self._cache

        self._cache = self._score_coins(texts, src_count)
        self._fetched_at = now
        return self._cache

    def index_for(self, symbol: str) -> SentimentIndex:
        """Return the sentiment index for a trading symbol (e.g. XRP/USD)."""
        base = symbol.split("/")[0].upper()
        if not self.cfg.enabled:
            return SentimentIndex(coin=base)
        self.refresh()
        return self._cache.get(base, SentimentIndex(coin=base))

    def should_block_buy(self, symbol: str) -> bool:
        return self.index_for(symbol).blocks_buy(self.cfg.buy_block_threshold)

    def should_force_exit(self, symbol: str) -> bool:
        return self.index_for(symbol).triggers_exit(self.cfg.exit_floor)

    # ── fetching ────────────────────────────────────────────────

    def _get(self, url: str) -> Optional[str]:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "DublinTradingAgent/1.0 (+sentiment)"},
            )
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as r:
                return r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                ValueError, Exception):
            return None

    def _fetch_rss(self, url: str) -> List[str]:
        raw = self._get(url)
        if not raw:
            return []
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return []
        out: List[str] = []
        for item in root.iter("item"):
            out.append(self._text_of(item))
        # Atom uses <entry><title>/<summary>
        if not out:
            for entry in root.iter("entry"):
                out.append(self._text_of(entry))
        return [t for t in out if t]

    def _fetch_reddit(self, sub: str) -> List[str]:
        url = f"https://www.reddit.com/r/{sub}/hot.json?limit=25"
        raw = self._get(url)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        out: List[str] = []
        for child in data.get("data", {}).get("children", []):
            post = child.get("data", {})
            title = post.get("title", "")
            selftext = post.get("selftext", "")
            combined = f"{title} {selftext}".strip()
            if combined:
                out.append(combined)
        return out

    @staticmethod
    def _text_of(el) -> str:
        parts = []
        for tag in ("title", "description", "summary", "content"):
            node = el.find(tag)
            if node is not None and node.text:
                parts.append(node.text)
        # Also grab any <content:encoded> style via tail/text fallback.
        if not parts:
            if el.text:
                parts.append(el.text)
        return _clean(" ".join(parts))

    # ── scoring ─────────────────────────────────────────────────

    def _score_coins(self, texts: List[str], src_count: int) -> Dict[str, SentimentIndex]:
        # Accumulate raw sentiment per coin and total.
        coin_score: Dict[str, float] = {}
        coin_n: Dict[str, int] = {}
        for text in texts:
            low = text.lower()
            s = self._score_text(low)
            # Attribute to every coin mentioned in the text.
            mentioned = [c for c, al in _COIN_ALIASES.items()
                         if any(a in low for a in al)]
            targets = mentioned or ["BTC"]  # unattributed news → market proxy
            for c in targets:
                coin_score[c] = coin_score.get(c, 0.0) + s
                coin_n[c] = coin_n.get(c, 0) + 1

        result: Dict[str, SentimentIndex] = {}
        for c, n in coin_n.items():
            # Average then squash to [-1, 1] with a soft tanh-like curve.
            avg = coin_score[c] / max(n, 1)
            score = max(-1.0, min(1.0, avg / (abs(avg) + 2.0)))
            result[c] = SentimentIndex(
                coin=c, score=round(score, 3), sample_size=n,
                source_count=src_count, fresh=True,
            )
        return result

    @staticmethod
    def _score_text(text: str) -> float:
        """Lexicon scorer with simple negation handling. Returns raw sum."""
        tokens = re.findall(r"[a-z\.]+", text)
        score = 0.0
        prev = ""
        for tok in tokens:
            w = 0.0
            if tok in _BULL:
                w = _BULL[tok]
            elif tok in _BEAR:
                w = _BEAR[tok]
            if w != 0.0 and prev in _NEGATIONS:
                w = -w  # "not bullish" → bearish
            score += w
            prev = tok
        return score


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)        # strip tags
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()
