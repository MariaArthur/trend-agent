"""
tools.py
--------
Real-data backend for the market trend research agent. Unlike a demo built
on hardcoded fixtures, every fact this agent reports is pulled live:

- search_web / build_trend_report / compare_topics hit the real Tavily
  search API (a search API built specifically for LLM agents).
- Sentiment scoring runs a real (local, offline) NLP model - VADER - over
  the retrieved text, not a guess by the LLM.
- Watchlist data persists in a real SQLite database on disk, so repeated
  scans build an actual time series you can chart.

Design principle (carried over, more important here than ever): the agent
must never assert a "trend" without grounding. Every report below returns
an explicit `confidence` field based on how many independent sources back
it up, and the system prompt (see agent.py) requires the agent to surface
that confidence rather than overstate a thin result as a strong trend.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

from dotenv import load_dotenv
from tavily import TavilyClient
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# Load key=value pairs from a local .env file (if present) into os.environ.
# Doesn't override a key that's already set in the real environment, and is
# a no-op (safely) if no .env file exists.
load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "watchlist.db")

_sentiment_analyzer = SentimentIntensityAnalyzer()


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------
def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist (
            topic TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trend_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            run_at TEXT NOT NULL,
            total_sources INTEGER,
            unique_domains INTEGER,
            avg_sentiment REAL,
            sentiment_label TEXT,
            confidence TEXT,
            summary TEXT,
            sources_json TEXT
        )
        """
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Tavily client
# ---------------------------------------------------------------------------
def _get_tavily():
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        raise RuntimeError(
            "TAVILY_API_KEY environment variable is not set. Get a free key at "
            "https://tavily.com and set it before running the app."
        )
    return TavilyClient(api_key=api_key)


# ---------------------------------------------------------------------------
# Sentiment
# ---------------------------------------------------------------------------
def _sentiment_label(compound: float) -> str:
    if compound >= 0.05:
        return "positive"
    if compound <= -0.05:
        return "negative"
    return "neutral"


def _score_texts(texts: list[str]) -> tuple[float, str]:
    """Average VADER compound score across a list of text snippets."""
    if not texts:
        return 0.0, "neutral"
    scores = [_sentiment_analyzer.polarity_scores(t)["compound"] for t in texts]
    avg = sum(scores) / len(scores)
    return round(avg, 3), _sentiment_label(avg)


# ---------------------------------------------------------------------------
# Tool 1: search_web - quick, single-purpose lookup
# ---------------------------------------------------------------------------
def search_web(query: str, max_results: int = 5) -> dict:
    """General-purpose web search for a quick factual question (not a full
    trend report). Use build_trend_report instead when the user wants a
    trend/market read, not a single fact."""
    client = _get_tavily()
    resp = client.search(query=query, max_results=max_results)
    results = [
        {
            "title": r.get("title"),
            "url": r.get("url"),
            "snippet": (r.get("content") or "")[:300],
        }
        for r in resp.get("results", [])
    ]
    return {
        "query": query,
        "answer": resp.get("answer"),
        "results": results,
        "result_count": len(results),
    }


# ---------------------------------------------------------------------------
# Tool 2: build_trend_report - the core research function
# ---------------------------------------------------------------------------
def build_trend_report(topic: str) -> dict:
    """Run a full trend research pass on a topic: general web + news search,
    sentiment scoring, source-diversity confidence check. This is the main
    tool for 'what's trending in X' / 'how is X being talked about' type
    questions."""
    client = _get_tavily()

    general = client.search(query=topic, max_results=6)
    news = client.search(query=topic, topic="news", max_results=6)

    all_results = (general.get("results") or []) + (news.get("results") or [])

    if not all_results:
        return {
            "topic": topic,
            "found": False,
            "confidence": "none",
            "error": "No search results found for this topic.",
        }

    texts = [r.get("content", "") for r in all_results if r.get("content")]
    avg_sentiment, sentiment_label = _score_texts(texts)

    domains = {urlparse(r.get("url", "")).netloc for r in all_results if r.get("url")}
    unique_domains = len(domains)
    total_sources = len(all_results)

    if unique_domains >= 4 and total_sources >= 6:
        confidence = "high"
    elif unique_domains >= 2 and total_sources >= 3:
        confidence = "medium"
    else:
        confidence = "low"

    # dedupe by URL, keep top sources for citation
    seen_urls = set()
    key_sources = []
    for r in all_results:
        url = r.get("url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        key_sources.append(
            {
                "title": r.get("title"),
                "url": url,
                "domain": urlparse(url).netloc,
                "snippet": (r.get("content") or "")[:250],
            }
        )
        if len(key_sources) >= 8:
            break

    return {
        "topic": topic,
        "found": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_sources": total_sources,
        "unique_domains": unique_domains,
        "avg_sentiment": avg_sentiment,
        "sentiment_label": sentiment_label,
        "confidence": confidence,
        "confidence_note": {
            "high": "Backed by many independent sources - reasonable to state findings plainly.",
            "medium": "Backed by a handful of sources - state findings with light hedging.",
            "low": "Backed by very few or overlapping sources - explicitly flag this as a thin signal, not a confirmed trend.",
            "none": "No data found.",
        }[confidence],
        "key_sources": key_sources,
        "tavily_answer": general.get("answer") or news.get("answer"),
    }


# ---------------------------------------------------------------------------
# Tool 3: compare_topics - side-by-side (e.g. competitor comparison)
# ---------------------------------------------------------------------------
def compare_topics(topic_a: str, topic_b: str) -> dict:
    """Build a trend report for two topics (e.g. two competing brands or
    products) and return them side by side for comparison."""
    report_a = build_trend_report(topic_a)
    report_b = build_trend_report(topic_b)
    return {"topic_a": report_a, "topic_b": report_b}


# ---------------------------------------------------------------------------
# Tool 4-7: watchlist management
# ---------------------------------------------------------------------------
def add_to_watchlist(topic: str) -> dict:
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (topic, created_at) VALUES (?, ?)",
            (topic, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return {"success": True, "topic": topic, "message": f"'{topic}' added to watchlist."}
    finally:
        conn.close()


def remove_from_watchlist(topic: str) -> dict:
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM watchlist WHERE topic = ?", (topic,))
        conn.commit()
        if cur.rowcount == 0:
            return {"success": False, "error": f"'{topic}' was not on the watchlist."}
        return {"success": True, "topic": topic, "message": f"'{topic}' removed from watchlist."}
    finally:
        conn.close()


def list_watchlist() -> dict:
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT topic, created_at FROM watchlist ORDER BY created_at").fetchall()
        topics = []
        for row in rows:
            latest = conn.execute(
                "SELECT run_at, avg_sentiment, sentiment_label, confidence, total_sources "
                "FROM trend_snapshots WHERE topic = ? ORDER BY run_at DESC LIMIT 1",
                (row["topic"],),
            ).fetchone()
            topics.append(
                {
                    "topic": row["topic"],
                    "added_at": row["created_at"],
                    "latest_snapshot": dict(latest) if latest else None,
                }
            )
        return {"watchlist": topics, "count": len(topics)}
    finally:
        conn.close()


def get_watchlist_history(topic: str, limit: int = 20) -> dict:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT run_at, total_sources, unique_domains, avg_sentiment, sentiment_label, confidence, summary "
            "FROM trend_snapshots WHERE topic = ? ORDER BY run_at ASC LIMIT ?",
            (topic, limit),
        ).fetchall()
        if not rows:
            return {"topic": topic, "found": False, "error": "No snapshot history for this topic yet."}
        return {"topic": topic, "found": True, "snapshots": [dict(r) for r in rows]}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 8: run_watchlist_scan - the "scheduled job" tool
# ---------------------------------------------------------------------------
def run_watchlist_scan() -> dict:
    """Re-run a trend check for every topic on the watchlist right now, and
    store a timestamped snapshot for each so trend-over-time history builds
    up. In production this same function is what a cron job / scheduled
    task would call automatically (see scan_watchlist.py)."""
    conn = _get_conn()
    try:
        topics = [r["topic"] for r in conn.execute("SELECT topic FROM watchlist").fetchall()]
        if not topics:
            return {"success": True, "scanned": 0, "message": "Watchlist is empty - nothing to scan."}

        results = []
        for topic in topics:
            report = build_trend_report(topic)
            if not report.get("found"):
                results.append({"topic": topic, "success": False, "error": report.get("error")})
                continue

            summary = (
                f"{report['total_sources']} sources, {report['unique_domains']} unique domains, "
                f"sentiment {report['sentiment_label']} ({report['avg_sentiment']}), "
                f"confidence {report['confidence']}"
            )
            conn.execute(
                """
                INSERT INTO trend_snapshots
                    (topic, run_at, total_sources, unique_domains, avg_sentiment,
                     sentiment_label, confidence, summary, sources_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    topic,
                    report["generated_at"],
                    report["total_sources"],
                    report["unique_domains"],
                    report["avg_sentiment"],
                    report["sentiment_label"],
                    report["confidence"],
                    summary,
                    json.dumps(report["key_sources"]),
                ),
            )
            results.append({"topic": topic, "success": True, "summary": summary})
        conn.commit()
        return {"success": True, "scanned": len(topics), "results": results}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool schemas (Groq / OpenAI-style function-calling format)
# ---------------------------------------------------------------------------
def _tool(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL_SCHEMAS = [
    _tool(
        "search_web",
        "Quick general web search for a single factual question. Use build_trend_report instead "
        "for 'what's trending' / market-read questions.",
        {"query": {"type": "string", "description": "The search query"}},
        ["query"],
    ),
    _tool(
        "build_trend_report",
        "Run a full trend research pass on a topic (general + news search, sentiment scoring, "
        "source-diversity confidence check). Use this for any 'what's trending in X' or "
        "'how is X being talked about' question.",
        {"topic": {"type": "string", "description": "The topic, product, brand, or industry to research"}},
        ["topic"],
    ),
    _tool(
        "compare_topics",
        "Build trend reports for two topics (e.g. two competing brands/products) and return them "
        "side by side for comparison.",
        {
            "topic_a": {"type": "string", "description": "First topic to compare"},
            "topic_b": {"type": "string", "description": "Second topic to compare"},
        },
        ["topic_a", "topic_b"],
    ),
    _tool(
        "add_to_watchlist",
        "Add a topic to the persistent watchlist for ongoing monitoring.",
        {"topic": {"type": "string", "description": "The topic to start monitoring"}},
        ["topic"],
    ),
    _tool(
        "remove_from_watchlist",
        "Remove a topic from the watchlist.",
        {"topic": {"type": "string", "description": "The topic to stop monitoring"}},
        ["topic"],
    ),
    _tool(
        "list_watchlist",
        "List all topics currently on the watchlist, with their latest snapshot if one exists.",
        {},
        [],
    ),
    _tool(
        "get_watchlist_history",
        "Get the full historical snapshot series for a watchlisted topic, to see how it has "
        "changed over time.",
        {"topic": {"type": "string", "description": "The watchlisted topic"}},
        ["topic"],
    ),
    _tool(
        "run_watchlist_scan",
        "Re-run a trend check right now for every topic on the watchlist, storing a new dated "
        "snapshot for each. Use when the user asks to refresh/update their watchlist.",
        {},
        [],
    ),
]

TOOL_FUNCTIONS = {
    "search_web": search_web,
    "build_trend_report": build_trend_report,
    "compare_topics": compare_topics,
    "add_to_watchlist": add_to_watchlist,
    "remove_from_watchlist": remove_from_watchlist,
    "list_watchlist": list_watchlist,
    "get_watchlist_history": get_watchlist_history,
    "run_watchlist_scan": run_watchlist_scan,
}


if __name__ == "__main__":
    # Standalone smoke test - run `python tools.py` to sanity check the real
    # API calls and DB logic before wiring up the LLM at all.
    print("== build_trend_report('electric bikes') ==")
    report = build_trend_report("electric bikes")
    print(json.dumps(report, indent=2)[:1500])

    print("\n== add_to_watchlist ==")
    print(add_to_watchlist("electric bikes"))

    print("\n== run_watchlist_scan ==")
    print(run_watchlist_scan())

    print("\n== list_watchlist ==")
    print(list_watchlist())

    print("\n== get_watchlist_history ==")
    print(get_watchlist_history("electric bikes"))
