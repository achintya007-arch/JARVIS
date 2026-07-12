"""
FastRouter — pre-LLM intent router.

Intercepts high-frequency, structurally predictable queries entirely with
regex. Zero latency — no LLM, no network, no model load.

Matched intents: time, system_info, weather (with city), timer (with duration)

Integrate before the LLM call in assistant.process_input().
Returns RouteResult on match, None to fall through to LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RouteResult:
    tool: str
    args: dict

    def to_executor_step(self) -> dict:
        return {"tool": self.tool, "args": self.args}


class FastRouter:

    # ── Intent patterns ───────────────────────────────────────────────────────

    _TIME = re.compile(
        r"\b(what(?:'s| is)(?: the)? time"
        r"|current time"
        r"|time (?:is it|now|right now)"
        r"|tell me the time"
        r"|what time is it)\b",
        re.IGNORECASE,
    )

    _SYSINFO = re.compile(
        r"\b(system (?:status|info|stats|health|usage|check)"
        r"|how(?:'s| is)(?: the)? (?:system|cpu|ram|gpu|memory|vram)"
        r"|(?:cpu|ram|gpu|vram|memory) (?:usage|load|status|temp|utilization)"
        r"|check (?:system|hardware|resources|status)"
        r"|what(?:'s| is)(?: the)? (?:cpu|ram|gpu|memory) (?:at|doing|like))\b",
        re.IGNORECASE,
    )

    _WEATHER = re.compile(
        r"(?:weather|forecast|temperature|temp)"
        r"(?:\s+(?:in|for|at|of))?\s+"
        r"(?P<city>[A-Za-z][\w\s]{1,30}?)"
        r"(?:\s*\?|$|\s+(?:today|now|right now|currently))",
        re.IGNORECASE,
    )
    # Fallback: "what's the weather?" with no city
    _WEATHER_BARE = re.compile(
        r"\b(what(?:'s| is)(?: the)? weather"
        r"|how(?:'s| is)(?: the)? weather"
        r"|weather (?:today|now|right now))\b",
        re.IGNORECASE,
    )

    _TIMER = re.compile(
        r"(?:set|start|create)?\s*"
        r"(?:a\s+)?(?:(?P<label>[a-z]+)\s+)?timer"
        r"\s+(?:for\s+)?(?P<duration>[0-9][^,;?!]*)"
        r"|timer\s+(?:for\s+)?(?P<duration2>[0-9][^,;?!]*)",
        re.IGNORECASE,
    )

    # ── STT normalization — fix common mishears before pattern matching ──────────
    _STT_REPLACEMENTS = [
        (re.compile(r"\bnew\s*tube\b",                re.IGNORECASE), "youtube"),
        (re.compile(r"\byou\s+tube\b",                re.IGNORECASE), "youtube"),
        (re.compile(r"\blink(?:ed)?\s*(?:in|din)\b",  re.IGNORECASE), "linkedin"),
        (re.compile(r"\bgoogle\s+chrome\b",           re.IGNORECASE), "chrome"),
        (re.compile(r"\bvisual\s+studio\s+code\b",    re.IGNORECASE), "vscode"),
        (re.compile(r"\bvs\s+code\b",                 re.IGNORECASE), "vscode"),
        (re.compile(r"\bvs\s+studio\b",               re.IGNORECASE), "vscode"),
    ]

    # ── Desktop app table: keyword → (Windows command, spoken name) ───────────
    _APP_MAP: dict[str, tuple[str, str]] = {
        "chrome":     ("start chrome",                                        "Chrome"),
        "notepad":    ("notepad",                                             "Notepad"),
        "calculator": ("calc",                                                "Calculator"),
        "todoist":    (r'start "" "%LOCALAPPDATA%\Todoist\Todoist.exe"',      "Todoist"),
        "spotify":    ("start spotify",                                       "Spotify"),
        "vscode":     ("code",                                                "VS Code"),
        "discord":    ("start discord",                                       "Discord"),
    }

    # ── URL table: keyword → (URL, spoken name) ───────────────────────────────
    _URL_MAP: dict[str, tuple[str, str]] = {
        "youtube":  ("https://youtube.com",      "YouTube"),
        "linkedin": ("https://linkedin.com",     "LinkedIn"),
        "google":   ("https://google.com",       "Google"),
        "gmail":    ("https://mail.google.com",  "Gmail"),
        "github":   ("https://github.com",       "GitHub"),
    }

    # Single launcher regex — only fires when the verb is the FIRST word.
    # Using ^ (not \b) prevents "remind me to open X" from triggering launch.
    _LAUNCH = re.compile(
        r"^(?:open|launch|start|play)\s+(?:up\s+)?(.+?)(?:\s+(?:please|now|for me))?\s*$",
        re.IGNORECASE,
    )

    # ── Volume ────────────────────────────────────────────────────────────────
    _VOLUME_SET = re.compile(
        r"\b(?:set|change|put|make)?\s*(?:the\s+)?volume\s+(?:to\s+)?(?P<level>\d{1,3})\s*(?:%|percent)?\b",
        re.IGNORECASE,
    )
    _VOLUME_UP = re.compile(
        r"\b(?:(?:turn|crank|bring|take)\s+)?(?:the\s+)?volume\s+up\b"
        r"|\bincrease\s+(?:the\s+)?volume\b"
        r"|\bvolume\s+(?:up|higher|louder)\b",
        re.IGNORECASE,
    )
    _VOLUME_DOWN = re.compile(
        r"\b(?:(?:turn|crank|bring|take)\s+)?(?:the\s+)?volume\s+down\b"
        r"|\b(?:decrease|lower|reduce)\s+(?:the\s+)?volume\b"
        r"|\bvolume\s+(?:down|lower|quieter|softer)\b",
        re.IGNORECASE,
    )
    _VOLUME_GET = re.compile(
        r"\b(?:what(?:'s| is)(?: the)? volume|current volume|volume level|how loud)\b",
        re.IGNORECASE,
    )
    _MUTE = re.compile(
        r"\b(?:mute|silence|shut\s+(?:it\s+)?up)\b(?!\s+(?:the\s+)?(?:timer|alarm))",
        re.IGNORECASE,
    )
    _UNMUTE = re.compile(
        r"\b(?:unmute|un-mute|unsilence|restore\s+(?:the\s+)?(?:volume|audio|sound))\b",
        re.IGNORECASE,
    )

    # ── Goals / reminders ─────────────────────────────────────────────────────
    _LIST_GOALS = re.compile(
        r"\b(?:"
        r"(?:show|list|what are|tell me)(?: me)?(?: my)? (?:goals?|reminders?|tasks?|objectives?)"
        r"|(?:my|any) (?:active )?(?:goals?|reminders?|tasks?)"
        r"|what (?:goals?|reminders?|tasks?) (?:do i have|have i got)"
        r")\b",
        re.IGNORECASE,
    )

    # ── Clipboard ─────────────────────────────────────────────────────────────
    _CLIPBOARD_GET = re.compile(
        r"\b(?:what(?:'s| is)(?: on)?(?: the)? clipboard"
        r"|(?:read|show|check|get)(?:\s+(?:me))?\s+(?:the\s+)?clipboard"
        r"|what did i copy"
        r"|show\s+clipboard)\b",
        re.IGNORECASE,
    )
    _CLIPBOARD_COPY = re.compile(
        r"\b(?:copy\s+(?:to\s+clipboard\s+)?|(?:put|add)\s+(?:on|to|in(?:to)?)\s+(?:the\s+)?clipboard\s+)(.+)$",
        re.IGNORECASE,
    )

    # ── Vault / notes (V3) ────────────────────────────────────────────────────
    _LIST_TODAY = re.compile(
        r"\bwhat\s+did\s+(?:i|we)\s+(?:do|talk\s+about|discuss)\s+today\b"
        r"|\bread\s+(?:my\s+)?(?:today'?s?\s+)?(?:daily\s+)?note\b"
        r"|\bwhat'?s\s+in\s+today'?s\s+note\b"
        r"|\bsummar(?:ize|y)\s+(?:of\s+)?(?:my\s+)?(?:day|today)\b",
        re.IGNORECASE,
    )
    _SEARCH_VAULT = re.compile(
        r"\bwhat\s+do\s+my\s+notes\s+say\s+about\s+(?P<q>.+)$"
        r"|\b(?:search|look\s+up|find|check)\s+(?:my\s+|the\s+)?(?:notes?|vault)\s+(?:for\s+|about\s+)?(?P<q2>.+)$"
        r"|\b(?:search|find)\s+(?:for\s+)?(?P<q3>.+?)\s+in\s+my\s+(?:notes?|vault)$",
        re.IGNORECASE,
    )
    _CREATE_NOTE = re.compile(
        r"^(?:make|take|create|write|jot)\s+(?:down\s+)?(?:a\s+|an\s+)?note\s+"
        r"(?:that\s+|about\s+|on\s+|saying\s+)?(?P<body>.+)$"
        r"|^note\s+(?:that\s+|down\s+)(?P<body2>.+)$",
        re.IGNORECASE,
    )
    _APPEND_DAILY = re.compile(
        r"^log\s+(?:this|that)?\s*[:\-]?\s*(?P<text>.+)$"
        r"|^add\s+to\s+(?:my\s+)?(?:daily\s+)?(?:note|journal|log)\s*[:\-]?\s*(?P<text2>.+)$",
        re.IGNORECASE,
    )

    # Conjunction splitter for compound commands
    _CONJ_SPLIT = re.compile(
        r"\s+(?:and(?:\s+then)?|then|also|after\s+that)\s+",
        re.IGNORECASE,
    )

    # Duration unit parsers — ordered by precedence
    _DURATION_PARTS = [
        (re.compile(r"(\d+)\s*h(?:our)?s?",     re.IGNORECASE), 3600),
        (re.compile(r"(\d+)\s*m(?:in(?:ute)?s?)?", re.IGNORECASE), 60),
        (re.compile(r"(\d+)\s*s(?:ec(?:ond)?s?)?", re.IGNORECASE), 1),
    ]

    # Labels that aren't actually timer names
    _FILLER_LABELS = frozenset({"a", "an", "the", "my", "timer", ""})

    # ── Public API ────────────────────────────────────────────────────────────

    def _normalize(self, text: str) -> str:
        """Lowercase + apply STT mishear corrections before regex matching."""
        text = text.strip().lower()
        for pattern, replacement in self._STT_REPLACEMENTS:
            text = pattern.sub(replacement, text)
        return text

    def route(self, text: str):
        text = self._normalize(text)
        parts = self._CONJ_SPLIT.split(text)
        if len(parts) > 1:
            routes = [r for p in parts if (r := self._route_single(p)) is not None]
            if len(routes) > 1:
                return routes        # compound → list[RouteResult]
            if len(routes) == 1:
                return routes[0]
            return None
        return self._route_single(text)

    def _route_single(self, text: str) -> RouteResult | None:
        """Single-intent routing on already-normalized text."""

        if self._TIME.search(text):
            return RouteResult(tool="get_time", args={})

        if self._SYSINFO.search(text):
            return RouteResult(tool="system_info", args={})

        m = self._WEATHER.search(text)
        if m:
            city = m.group("city").strip().rstrip("?").strip()
            return RouteResult(tool="weather", args={"city": city or "bangalore"})

        if self._WEATHER_BARE.search(text):
            return RouteResult(tool="weather", args={"city": "bangalore"})

        m = self._TIMER.search(text)
        if m:
            duration_str = (m.group("duration") or m.group("duration2") or "").strip()
            label        = (m.group("label") or "").strip().lower()
            if label in self._FILLER_LABELS:
                label = "timer"
            seconds = self._parse_duration(duration_str)
            if seconds and seconds > 0:
                return RouteResult(
                    tool="set_timer",
                    args={"duration_seconds": seconds, "label": label},
                )

        # ── Goals ─────────────────────────────────────────────────────────
        if self._LIST_GOALS.search(text):
            return RouteResult(tool="list_goals", args={})

        # ── Volume ────────────────────────────────────────────────────────
        m = self._VOLUME_SET.search(text)
        if m:
            return RouteResult(tool="set_volume", args={"level": int(m.group("level"))})

        if self._VOLUME_UP.search(text):
            return RouteResult(tool="set_volume", args={"delta": 10})

        if self._VOLUME_DOWN.search(text):
            return RouteResult(tool="set_volume", args={"delta": -10})

        if self._VOLUME_GET.search(text):
            return RouteResult(tool="get_volume", args={})

        if self._MUTE.search(text):
            return RouteResult(tool="mute_volume", args={})

        if self._UNMUTE.search(text):
            return RouteResult(tool="unmute_volume", args={})

        # ── Clipboard ─────────────────────────────────────────────────────
        if self._CLIPBOARD_GET.search(text):
            return RouteResult(tool="get_clipboard", args={})

        m = self._CLIPBOARD_COPY.search(text)
        if m:
            return RouteResult(tool="set_clipboard", args={"text": m.group(1).strip()})

        # ── Vault / notes (V3) — search/list before create so "search my notes"
        #    is never mistaken for authoring a note ─────────────────────────
        if self._LIST_TODAY.search(text):
            return RouteResult(tool="list_today", args={})

        m = self._SEARCH_VAULT.search(text)
        if m:
            q = (m.group("q") or m.group("q2") or m.group("q3") or "").strip()
            if q:
                return RouteResult(tool="search_vault", args={"query": q})

        m = self._CREATE_NOTE.search(text)
        if m:
            body = (m.group("body") or m.group("body2") or "").strip()
            if body:
                return RouteResult(tool="create_note", args={"title": body[:60], "body": body})

        m = self._APPEND_DAILY.search(text)
        if m:
            txt = (m.group("text") or m.group("text2") or "").strip()
            if txt:
                return RouteResult(tool="append_to_daily_note", args={"text": txt})

        # Bare target — handles compound split arms with no verb (e.g. "spotify", "notepad")
        if text in self._APP_MAP:
            cmd, name = self._APP_MAP[text]
            return RouteResult(tool="open_app", args={"command": cmd, "name": name})
        if text in self._URL_MAP:
            url, name = self._URL_MAP[text]
            return RouteResult(tool="open_url", args={"url": url, "name": name})

        # Universal launcher — one pattern, table-driven dispatch
        m = self._LAUNCH.search(text)
        if m:
            target = m.group(1).strip()
            if target in self._URL_MAP:
                url, name = self._URL_MAP[target]
                return RouteResult(tool="open_url", args={"url": url, "name": name})
            if target in self._APP_MAP:
                cmd, name = self._APP_MAP[target]
                return RouteResult(tool="open_app", args={"command": cmd, "name": name})
            for key, (cmd, name) in self._APP_MAP.items():
                if key in target:
                    return RouteResult(tool="open_app", args={"command": cmd, "name": name})
            for key, (url, name) in self._URL_MAP.items():
                if key in target:
                    return RouteResult(tool="open_url", args={"url": url, "name": name})

        return None

    # ── Duration parser ───────────────────────────────────────────────────────

    def _parse_duration(self, text: str) -> int | None:
        """
        "10 minutes"          → 600
        "2 hours 30 minutes"  → 9000
        "90 seconds"          → 90
        "1h30m"               → 5400
        "half an hour"        → 1800
        """
        # Special cases
        low = text.lower().strip()
        if re.search(r"half\s+an?\s+hour", low):
            return 1800
        if re.search(r"quarter\s+(?:of\s+an?\s+)?hour", low):
            return 900

        # Fix #4: use findall() so multiple units in the same string are all summed.
        # search() only found the first match per unit, so "1h 30m" gave 3600 not 5400.
        total = 0
        matched = False
        for pattern, multiplier in self._DURATION_PARTS:
            for m in pattern.finditer(text):
                total += int(m.group(1)) * multiplier
                matched = True

        return total if matched else None
