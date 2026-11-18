"""Google web search using SerpApi.

Returns public web results in the context format used by the RAG agent.
API keys are never logged or included in error messages.
"""

import logging
import re
from urllib.parse import urlparse

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
MAX_QUERY_LENGTH = 300
MAX_RESULTS = 5
REQUEST_TIMEOUT_SECONDS = 10.0


def _valid_http_url(value: object) -> bool:
    """Accept only ordinary HTTP/HTTPS URLs with a hostname."""
    if not isinstance(value, str) or not value.strip():
        return False

    try:
        parsed = urlparse(value.strip())
        return (
            parsed.scheme.lower() in {"http", "https"}
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
        )
    except (TypeError, ValueError):
        return False


def clean_search_query(query: str) -> str:
    """Remove generic requests for sources that can distract search engines."""
    query = " ".join((query or "").split())
    query = re.sub(
        r"\s*(?:please\s+)?(?:provide|give|include)\s+"
        r"(?:me\s+)?(?:the\s+)?(?:reliable\s+|relevant\s+)?"
        r"sources?(?:\s+and\s+links?)?.*$",
        "",
        query,
        flags=re.IGNORECASE,
    )
    return query.strip(" .,:;!?")

STOP_WORDS = {
    "what", "is", "the", "a", "an", "how", "does",
    "work", "latest", "best", "in", "and", "of",
}

OFFICIAL_DOMAINS = {
    "python.org",
    "devguide.python.org",
    "docs.python.org",
}


def score_search_result(query: str, title: str, snippet: str, url: str) -> float:
    """Score query relevance using weighted title/snippet matches."""
    query_terms = set(re.findall(r"[a-z0-9]+", query.lower())) - STOP_WORDS

    if not query_terms:
        return 0.0

    title_terms = set(re.findall(r"[a-z0-9]+", title.lower()))
    snippet_terms = set(re.findall(r"[a-z0-9]+", snippet.lower()))

    title_match = len(query_terms & title_terms) / len(query_terms)
    snippet_match = len(query_terms & snippet_terms) / len(query_terms)

    # Title matches count more than snippet matches.
    score = (0.70 * title_match) + (0.30 * snippet_match)

    host = (urlparse(url).hostname or "").lower()
    if any(
        host == domain or host.endswith("." + domain)
        for domain in OFFICIAL_DOMAINS
    ):
        score += 0.05

    return round(min(score, 1.0), 3)

def should_search_web(question: str) -> bool:
    """Route public questions to web search; preserve explicit private-only requests."""
    q = " ".join((question or "").lower().split())

    if not q:
        return False

    # Explicit instructions against web search take priority.
    no_web_cues = (
        "do not search the web", "dont search the web",
        "do not use web search", "without searching the web",
        "no web search", "use only this uploaded document",
        "use only the uploaded document",
        "answer only from this uploaded document",
    )
    if any(cue in q for cue in no_web_cues):
        return False

    # Suppress web search for explicit private-document requests.
    private_only = (
        "summarize my uploaded document",
        "summarize my uploaded file",
        "summarize my pdf",
        "what does my uploaded document say",
        "what does my uploaded file say",
        "according to my uploaded document",
        "according to my uploaded file",
        "answer only from my document",
        "answer only from the uploaded document",
        "search only my uploaded documents",
        "find this in my uploaded document",
        "from my uploaded pdf",
    )

    if any(phrase in q for phrase in private_only):
        return False

    # Current or rapidly changing public information.
    current_cues = (
        "latest", "current", "currently", "today", "right now",
        "recent", "this week", "this month", "this year",
        "news", "weather", "price", "prices", "release",
        "version", "updated", "stock", "exchange rate",
        "opening hours", "open now", "forecast",
    )

    # Local businesses, places, travel and service discovery.
    local_cues = (
        "near me", "nearby", "location", "locations",
        "in bengaluru", "in bangalore", "in brigade road",
        "tea shop", "tea shops", "coffee shop", "coffee shops",
        "cafe", "cafes", "restaurant", "restaurants",
        "hotel", "hotels", "shop", "shops", "store", "stores",
        "hospital", "hospitals", "school", "schools",
        "college", "colleges", "university", "universities",
        "places to visit", "tourist places", "travel",
        "where can i find", "businesses near",
        "directions to", "best places", "best hotels",
    )

    # Common forms of general-information questions.
    general_starts = (
        "what ", "what's ", "whats ", "who ", "whom ", "whose ",
        "where ", "when ", "why ", "how ", "which ",
        "explain ", "define ", "describe ", "compare ",
        "difference between ", "advantages of ", "disadvantages of ",
        "pros and cons of ", "list ", "show me ", "find ",
        "search for ", "tell me about ", "give me information about ",
        "give information about ", "recommend ",
        "recommendations for ", "top ", "best ",
        "examples of ", "history of ", "meaning of ",
        "steps to ", "guide to ", "tutorial for ",
    )

    if any(cue in q for cue in current_cues + local_cues):
        return True

    if q.startswith(general_starts):
        return True

    # If the user explicitly asks to combine private documents and public
    # information, allow web search as well.
    mixed_source_cues = (
        "compare my document with",
        "compare my pdf with",
        "compare my file with",
        "along with latest",
        "alongside current",
        "latest industry trends",
        "external sources",
        "web sources",
        "search the web",
        "search online",
        "from the internet",
    )

    return any(cue in q for cue in mixed_source_cues)


async def google_search(query: str) -> list[dict]:
    """Search Google and return deduplicated, RAG-compatible web contexts."""
    api_key = (settings.serpapi_api_key or "").strip()

    if not api_key:
        logger.warning("Web search skipped: SerpApi API key is not configured.")
        return []

    query = clean_search_query(query)[:MAX_QUERY_LENGTH]
    if not query:
        return []

    params = {
        "engine": "google",
        "q": query,
        "api_key": api_key,
        "num": MAX_RESULTS,
        "hl": "en",
        "gl": "in",
    }

    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
        ) as client:
            response = await client.get(SERPAPI_ENDPOINT, params=params)
            response.raise_for_status()
            payload = response.json()

        if not isinstance(payload, dict):
            logger.warning("Web search returned an unexpected response format.")
            return []

        results = payload.get("organic_results", [])
        if not isinstance(results, list):
            return []

        contexts = []
        seen_urls = set()

        for position, item in enumerate(results, start=1):
            if not isinstance(item, dict):
                continue

            title = str(item.get("title") or "").strip()
            snippet = str(item.get("snippet") or "").strip()
            url = str(item.get("link") or "").strip()

            if not _valid_http_url(url):
                continue

            # Avoid duplicate URLs, including fragments and trailing slashes.
            parsed = urlparse(url)
            normalized_url = (
                f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
                f"{parsed.path.rstrip('/')}"
            )

            if normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)

            if not title and not snippet:
                continue

            evidence_parts = []
            if title:
                evidence_parts.append(f"Title: {title}")
            if snippet:
                evidence_parts.append(f"Snippet: {snippet}")

            relevance_score = score_search_result(query, title, snippet, url)

            contexts.append({
                "text": "\n".join(evidence_parts),
                "metadata": {
                    "source_type": "web",
                    "title": title or url,
                    "filename": title or url,
                    "url": url,
                    "snippet": snippet,
                    "position": position,
                    "search_query": query,
                    "relevance_score": relevance_score,
                },
            })

            if len(contexts) >= MAX_RESULTS:
                break

        # Keep all valid results, prioritizing the strongest keyword matches.
        contexts.sort(
            key=lambda context: context["metadata"].get("relevance_score", 0),
            reverse=True,
        )

        return contexts

    except (httpx.HTTPError, ValueError) as exc:
        # Never log request URLs or parameters because they contain the API key.
        logger.warning(
            "Web search failed (%s). Continuing without web results.",
            type(exc).__name__,
        )
        return []
    except Exception as exc:
        # Keep unexpected errors from exposing request details or credentials.
        logger.warning(
            "Unexpected web search error (%s). Continuing without web results.",
            type(exc).__name__,
        )
        return []
