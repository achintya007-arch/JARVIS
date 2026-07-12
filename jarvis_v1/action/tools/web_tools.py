"""
Web tools — async HTTP with httpx.

Sprint A fix: replaced sync `requests` with async `httpx`.
Sync calls were blocking the entire event loop during weather/search.
"""

from __future__ import annotations
import logging
from typing import Optional

import httpx

log = logging.getLogger("jarvis.web_tools")

# Lazily created so the client binds to the running event loop on first use
# rather than at import time (which caused "Event loop is closed" across loop
# restarts and leaked the connection pool). Reused across calls thereafter.
_CLIENT: Optional[httpx.AsyncClient] = None


def _client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None or _CLIENT.is_closed:
        _CLIENT = httpx.AsyncClient(timeout=8.0)
    return _CLIENT


async def close_client() -> None:
    """Close the shared client. Call during shutdown to release the pool."""
    global _CLIENT
    if _CLIENT is not None and not _CLIENT.is_closed:
        await _CLIENT.aclose()
    _CLIENT = None


async def get_weather(city: str) -> str:
    try:
        resp = await _client().get(f"https://wttr.in/{city}?format=3")
        resp.raise_for_status()
        return resp.text.strip()
    except httpx.TimeoutException:
        return f"Weather request timed out for {city}."
    except Exception as e:
        log.warning("Weather fetch failed: %s", e)
        return "Unable to fetch weather at this time."


async def web_search(query: str) -> str:
    try:
        resp = await _client().get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_redirect": "1"},
        )
        resp.raise_for_status()
        data = resp.json()
        abstract = data.get("AbstractText", "").strip()
        if abstract:
            return abstract
        topics = data.get("RelatedTopics", [])
        if topics and isinstance(topics[0], dict):
            return topics[0].get("Text", "No result found.")
        return "No result found for that query."
    except httpx.TimeoutException:
        return "Search request timed out."
    except Exception as e:
        log.warning("Web search failed: %s", e)
        return "Search failed."