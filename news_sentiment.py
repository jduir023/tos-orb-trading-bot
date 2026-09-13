"""
news_sentiment.py
Rule-based headline scoring for trade gating.
Fail-open when fetch fails or no recent headlines.
"""

import json
import os
import re
import time
import urllib.request
from typing import Any, Dict, List

# Instant block patterns — negative catalyst regardless of aggregate score
HARD_BLOCK_PATTERNS = [
    r"\bsecondary offering\b",
    r"\bpublic offering\b",
    r"\bat-the-market\b",
    r"\batm offering\b",
    r"\bstock offering\b",
    r"\bregistered direct\b",
    r"\bdilution\b",
    r"\bsec investigation\b",
    r"\bsecurities fraud\b",
    r"\bfraud charge\b",
    r"\bbankruptcy\b",
    r"\bchapter 11\b",
    r"\bchapter 7\b",
    r"\bclass action\b",
    r"\bgoing concern\b",
    r"\breceives delisting\b",
    r"\bdelisting notice\b",
    r"\bnot in compliance\b",
    r"\brestatement\b",
    r"\baccounting irregularit",
    r"\bwells notice\b",
    r"\bfd?a warning\b",
    r"\bclinical hold\b",
    r"\btrial failure\b",
    r"\bphase [123] fail",
    r"\bshort report\b",
    r"\bshort seller\b",
    r"\bhindenburg\b",
    r"\bearnings miss\b",
    r"\brevenue miss\b",
    r"\bguidance cut\b",
    r"\blowers guidance\b",
]

BEARISH_KEYWORDS = {
    "offering": -45,
    "dilution": -50,
    "investigation": -40,
    "lawsuit": -35,
    "bankruptcy": -60,
    "fraud": -55,
    "delisting": -45,
    "downgrade": -30,
    "warning": -25,
    "decline": -15,
    "plunge": -25,
    "tumble": -25,
    "suspend": -35,
    "layoff": -20,
    "default": -40,
    "probe": -30,
    "subpoena": -35,
    "resign": -20,
    "misses": -25,
    "cut": -20,
    "loss": -15,
}

BULLISH_KEYWORDS = {
    "beat": 25,
    "beats": 25,
    "surpass": 20,
    "exceed": 20,
    "upgrade": 30,
    "outperform": 20,
    "contract": 20,
    "partnership": 15,
    "approval": 35,
    "fda approval": 40,
    "breakthrough": 25,
    "buyout": 30,
    "record": 20,
    "profit": 15,
    "guidance raise": 25,
    "expansion": 15,
    "deal": 15,
    "award": 15,
}


class NewsSentimentService:
    """Fetch Yahoo headlines and score them for trade gating."""

    def __init__(
        self,
        data_dir: str = "saved_data",
        block_score: float = -40,
        cautious_score: float = -10,
        boost_score: float = 25,
        max_age_hours: float = 24,
        size_boost_pct: float = 0.15,
        cache_ttl_sec: float = 900,
    ) -> None:
        self.data_dir = data_dir
        self.block_score = block_score
        self.cautious_score = cautious_score
        self.boost_score = boost_score
        self.max_age_hours = max_age_hours
        self.size_boost_pct = size_boost_pct
        self.cache_ttl_sec = cache_ttl_sec
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ts: Dict[str, float] = {}
        self._load_disk_cache()

    def update_config(
        self,
        block_score: float = None,
        cautious_score: float = None,
        boost_score: float = None,
        max_age_hours: float = None,
        size_boost_pct: float = None,
        cache_ttl_sec: float = None,
    ) -> None:
        if block_score is not None:
            self.block_score = block_score
        if cautious_score is not None:
            self.cautious_score = cautious_score
        if boost_score is not None:
            self.boost_score = boost_score
        if max_age_hours is not None:
            self.max_age_hours = max_age_hours
        if size_boost_pct is not None:
            self.size_boost_pct = size_boost_pct
        if cache_ttl_sec is not None:
            self.cache_ttl_sec = cache_ttl_sec

    def _cache_path(self) -> str:
        return os.path.join(self.data_dir, "news_sentiment_cache.json")

    def _load_disk_cache(self) -> None:
        path = self._cache_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r") as f:
                data = json.load(f)
            self._cache = data.get("entries", {})
            self._cache_ts = {
                k: float(v.get("fetched_at", 0) or 0)
                for k, v in self._cache.items()
            }
        except Exception:
            pass

    def _save_disk_cache(self) -> None:
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self._cache_path(), "w") as f:
                json.dump({"entries": self._cache, "updated_at": time.time()}, f, indent=2)
        except Exception:
            pass

    def _fetch_headlines(self, symbol: str) -> List[Dict[str, Any]]:
        url = (
            "https://query1.finance.yahoo.com/v1/finance/search"
            f"?q={symbol}&quotesCount=0&newsCount=6&enableFuzzyQuery=false"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
        headlines: List[Dict[str, Any]] = []
        for item in data.get("news", [])[:6]:
            ts = item.get("providerPublishTime", 0)
            headlines.append({
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "source": item.get("publisher", ""),
                "published_at": float(ts) if ts else 0.0,
            })
        return headlines

    def score_headline(self, title: str) -> Dict[str, Any]:
        """Score a single headline — used by tests and aggregation."""
        title_lower = (title or "").lower()
        tags: List[str] = []

        for pattern in HARD_BLOCK_PATTERNS:
            if re.search(pattern, title_lower):
                return {
                    "score": -100,
                    "tags": ["hard_block"],
                    "blocked": True,
                    "size_mult": 0.0,
                    "headline": title,
                }

        score = 0
        for keyword, points in BEARISH_KEYWORDS.items():
            if keyword in title_lower:
                score += points
                tags.append(f"bear:{keyword}")

        for keyword, points in BULLISH_KEYWORDS.items():
            if keyword in title_lower:
                score += points
                tags.append(f"bull:{keyword}")

        score = max(-100, min(100, score))
        blocked = score <= self.block_score
        size_mult = 1.0
        if not blocked and score >= self.boost_score:
            size_mult = 1.0 + self.size_boost_pct

        return {
            "score": score,
            "tags": tags,
            "blocked": blocked,
            "size_mult": size_mult,
            "headline": title,
        }

    def _aggregate(self, headlines: List[Dict[str, Any]]) -> Dict[str, Any]:
        now = time.time()
        max_age_sec = self.max_age_hours * 3600.0

        recent = [
            h for h in headlines
            if h.get("published_at", 0) > 0
            and (now - float(h["published_at"])) <= max_age_sec
        ]
        if not recent:
            recent = headlines[:3]

        if not recent:
            return {
                "score": 0,
                "tags": [],
                "blocked": False,
                "size_mult": 1.0,
                "headline": "",
                "headlines": [],
                "fail_open": True,
            }

        worst: Dict[str, Any] = {
            "score": 0,
            "tags": [],
            "blocked": False,
            "size_mult": 1.0,
            "headline": "",
        }
        all_tags: List[str] = []

        for headline in recent:
            scored = self.score_headline(headline.get("title", ""))
            all_tags.extend(scored.get("tags", []))
            if scored.get("blocked"):
                return {
                    "score": scored["score"],
                    "tags": list(set(scored.get("tags", []) + all_tags)),
                    "blocked": True,
                    "size_mult": 0.0,
                    "headline": scored.get("headline", ""),
                    "headlines": recent,
                    "fail_open": False,
                }
            if scored["score"] < worst["score"]:
                worst = scored

        return {
            "score": worst["score"],
            "tags": list(set(all_tags)),
            "blocked": worst["score"] <= self.block_score,
            "size_mult": worst.get("size_mult", 1.0),
            "headline": worst.get("headline", ""),
            "headlines": recent,
            "fail_open": False,
        }

    def get_sentiment(self, symbol: str, force_refresh: bool = False) -> Dict[str, Any]:
        sym = (symbol or "").upper()
        if not sym:
            return {
                "symbol": "",
                "score": 0,
                "tags": [],
                "blocked": False,
                "size_mult": 1.0,
                "headline": "",
                "headlines": [],
                "fail_open": True,
            }

        now = time.time()
        if (
            not force_refresh
            and sym in self._cache
            and (now - self._cache_ts.get(sym, 0.0)) < self.cache_ttl_sec
        ):
            return dict(self._cache[sym])

        try:
            headlines = self._fetch_headlines(sym)
            result = self._aggregate(headlines)
            result["symbol"] = sym
            result["fetched_at"] = now
            self._cache[sym] = result
            self._cache_ts[sym] = now
            self._save_disk_cache()
            return result
        except Exception as exc:
            if sym in self._cache:
                cached = dict(self._cache[sym])
                cached["fail_open"] = True
                cached["fetch_error"] = str(exc)
                return cached
            return {
                "symbol": sym,
                "score": 0,
                "tags": [],
                "blocked": False,
                "size_mult": 1.0,
                "headline": "",
                "headlines": [],
                "fail_open": True,
                "fetched_at": now,
            }

    def set_cached_sentiment(self, symbol: str, sentiment: Dict[str, Any]) -> None:
        """Inject cached sentiment — used by tests."""
        sym = (symbol or "").upper()
        entry = dict(sentiment)
        entry["symbol"] = sym
        entry["fetched_at"] = time.time()
        self._cache[sym] = entry
        self._cache_ts[sym] = entry["fetched_at"]

    def refresh_symbols(self, symbols: List[str]) -> None:
        for symbol in symbols:
            if symbol:
                self.get_sentiment(symbol, force_refresh=True)