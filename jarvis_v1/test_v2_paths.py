"""
test_v2_paths.py — Phase 2 validation: all 4 decision paths.

Runs entirely offline — no LLM, no TTS, no Ollama required.
Tests DecisionEngine + FastRouter integration only.

Run:
    python test_v2_paths.py
"""

import asyncio
import sys

sys.path.insert(0, ".")

from action.fast_router import FastRouter
from cognition.agent_state import AgentState
from cognition.decision_engine import DecisionEngine

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def make_engine() -> tuple[DecisionEngine, AgentState]:
    router = FastRouter()
    engine = DecisionEngine(fast_router=router)
    state  = AgentState()
    return engine, state


async def decide(text: str) -> str:
    engine, agent_state = make_engine()
    agent_state.update_from_input(text)
    snap = agent_state.snapshot()
    d = await engine.decide(text=text, state=snap, memories=[], messages=[])
    return d.action_type, d.reason


async def route_direct(text: str):
    """Return raw FastRouter result for compound/fuzzy tests."""
    router = FastRouter()
    return router.route(text)


async def run_tests() -> None:
    results = []

    # ── PATH 1: tool ──────────────────────────────────────────────────────────
    cases_tool = [
        "what time is it",
        "what's the time",
        "system info",
        "how's the CPU",
        "open chrome",
        "open youtube",
        "open notepad",
        "open calculator",
        "open new tube",
        "open link din",
        "open google",
    ]
    for text in cases_tool:
        action, reason = await decide(text)
        ok = action == "tool"
        results.append((ok, "tool", text, action, reason))

    # ── PATH 2: speak (rule-based) ────────────────────────────────────────────
    cases_speak = [
        "thank you so much",
        "thanks",
        "cheers",
    ]
    for text in cases_speak:
        action, reason = await decide(text)
        ok = action == "speak"
        results.append((ok, "speak", text, action, reason))

    # ── PATH 3: ignore (low-signal) ───────────────────────────────────────────
    cases_ignore = [
        "ok",
        "hmm",
        "...",
        "eh",
        "uh",
        "ah",
    ]
    for text in cases_ignore:
        action, reason = await decide(text)
        ok = action == "ignore"
        results.append((ok, "ignore", text, action, reason))

    # ── PATH 4: llm (fallback) ────────────────────────────────────────────────
    cases_llm = [
        "explain quantum computing",
        "what's the meaning of life",
        "write me a haiku about rain",
        "how do neural networks work",
    ]
    for text in cases_llm:
        action, reason = await decide(text)
        ok = action == "llm"
        results.append((ok, "llm", text, action, reason))

    # ── PATH 5: fuzzy app names → still tool ─────────────────────────────────
    cases_fuzzy = [
        "open vs code",
        "open visual studio code",
    ]
    for text in cases_fuzzy:
        action, reason = await decide(text)
        ok = action == "tool"
        results.append((ok, "tool(fuzzy)", text, action, reason))

    # ── PATH 6: compound commands → plan ─────────────────────────────────────
    cases_plan = [
        "open chrome and open notepad",
        "open youtube and open spotify",
        "open vs code and play spotify",
    ]
    for text in cases_plan:
        action, reason = await decide(text)
        ok = action == "plan"
        results.append((ok, "plan", text, action, reason))

    # ── Print results ─────────────────────────────────────────────────────────
    print(f"\n{'-'*60}")
    print("  JARVIS V2 -- Decision Path Tests")
    print(f"{'-'*60}")

    current_path = None
    for ok, expected_path, text, got_action, reason in results:
        if expected_path != current_path:
            current_path = expected_path
            print(f"\n  [{expected_path.upper()}]")
        tag = PASS if ok else FAIL
        mark = "" if ok else f"  ← got '{got_action}' ({reason})"
        print(f"    {tag}  {text!r}{mark}")

    passed = sum(1 for r in results if r[0])
    total  = len(results)
    print(f"\n{'-'*60}")
    status = PASS if passed == total else FAIL
    print(f"  {status}  {passed}/{total} passed")
    print(f"{'-'*60}\n")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(run_tests())
