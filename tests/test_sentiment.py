"""Tests for the Stage-1 sentiment trading agent (offline / mocked network)."""

from __future__ import annotations


from dublin_bot.sentiment import (
    SentimentAgent,
    SentimentConfig,
    SentimentIndex,
)


def _fake_agent(articles, subreddit_posts):
    """Build an agent whose network calls return the supplied fixtures."""
    import json
    agent = SentimentAgent(SentimentConfig(enabled=True, cache_ttl_s=0))

    def fake_get(url):
        if "reddit.com" in url:
            payload = {"data": {"children": [
                {"data": {"title": t, "selftext": ""}} for t in subreddit_posts
            ]}}
            return json.dumps(payload)
        # RSS: wrap every article in a single <item><title>..</title></item>
        items = "".join(f"<item><title>{a}</title></item>" for a in articles)
        return f'<rss><channel>{items}</channel></rss>'

    agent._get = fake_get
    return agent


def test_lexicon_scores_bullish_and_bearish():
    agent = SentimentAgent(SentimentConfig(enabled=False))
    assert agent._score_text("bitcoin surge to the moon bullish breakout") > 0
    assert agent._score_text("scam rugpull crash dump bearish") < 0


def test_negation_flips_polarity():
    agent = SentimentAgent(SentimentConfig(enabled=False))
    bull = agent._score_text("this is bullish")
    not_bull = agent._score_text("this is not bullish")
    assert bull > 0
    assert not_bull < 0  # negation flips the sign


def test_bearish_coin_blocks_buy():
    # Heavy bearish XRP news -> index should block a BUY entry.
    articles = [
        "XRP scam rugpull crash dump bearish lawsuit sec warning",
        "Ripple XRP collapse plunge delisting panic",
    ] * 3
    agent = _fake_agent(articles, [])
    idx = agent.index_for("XRP/USD")
    assert idx.fresh is True
    assert idx.score < 0
    assert agent.should_block_buy("XRP/USD") is True


def test_bullish_coin_does_not_block_buy():
    articles = ["XRP breakout surge rally adoption partnership bullish moon"] * 3
    agent = _fake_agent(articles, [])
    idx = agent.index_for("XRP/USD")
    assert idx.score > 0
    assert agent.should_block_buy("XRP/USD") is False


def test_unattributed_news_attributes_to_market_proxy():
    # No coin named -> attributed to BTC (market proxy), never crashes.
    articles = ["global markets rally optimism green"] * 2
    agent = _fake_agent(articles, [])
    idx = agent.index_for("BTC/USD")
    assert idx.fresh is True
    assert "BTC" in agent._cache


def test_disabled_agent_returns_neutral():
    agent = SentimentAgent(SentimentConfig(enabled=False))
    idx = agent.index_for("XRP/USD")
    assert idx.score == 0.0
    assert idx.fresh is False
    assert agent.should_block_buy("XRP/USD") is False


def test_network_failure_degrades_to_neutral():
    agent = SentimentAgent(SentimentConfig(enabled=True, cache_ttl_s=0))
    agent._get = lambda url: None  # simulate total network failure
    res = agent.refresh()
    assert res == {}  # no stale/forced readings
    assert agent.index_for("XRP/USD").fresh is False


def test_reddit_json_parsed():
    posts = ["XRP pump rocket moon accumulate", "XRP dump crash rug"]
    agent = _fake_agent([], posts)
    idx = agent.index_for("XRP/USD")
    assert idx.fresh is True
    assert idx.sample_size >= 2


def test_sentiment_index_thresholds():
    neutral = SentimentIndex(coin="XRP", score=-0.10, fresh=True)
    bearish = SentimentIndex(coin="XRP", score=-0.30, fresh=True)
    collapsed = SentimentIndex(coin="XRP", score=-0.60, fresh=True)
    assert neutral.blocks_buy() is False
    assert bearish.blocks_buy() is True
    assert collapsed.triggers_exit() is True
    assert neutral.triggers_exit() is False
    # Non-fresh readings never act.
    assert SentimentIndex(coin="XRP", score=-0.9, fresh=False).blocks_buy() is False
