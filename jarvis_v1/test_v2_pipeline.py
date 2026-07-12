"""
test_v2_pipeline.py — End-to-end pipeline integration test (offline).

Validates: UserInput -> DecisionEngine -> AgentExecutor -> response
No LLM, no TTS, no STT, no Ollama required.

Measures per-step latency:
  [decide]  FastRouter + DecisionEngine
  [execute] AgentExecutor tool call
  [total]   wall-clock end-to-end

Usage:
    python test_v2_pipeline.py
"""

import asyncio
import sys
import time

sys.path.insert(0, ".")

from cognition.agent_state import AgentState
from cognition.decision_engine import DecisionEngine
from action.fast_router import FastRouter
from action.agent_executor import AgentExecutor
from core.config import AgentConfig

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
DIM  = "\033[2m"
RST  = "\033[0m"


def _make_components():
    router   = FastRouter()
    engine   = DecisionEngine(fast_router=router)
    state    = AgentState()
    executor = AgentExecutor(
        config=AgentConfig(
            confirmation_required=False,   # no interactive prompts in tests
            web_enabled=False,             # skip network tools
        )
    )
    return engine, state, executor


def _format(tool: str, result: dict) -> str:
    if result.get("status") == "error":
        return f"[error] {result.get('error', '')}"
    if result.get("status") == "denied":
        return f"[denied] {result.get('reason', '')}"
    r = result.get("result", "")
    if tool == "get_time":    return f"It is {r}, Sir."
    if tool == "system_info": return str(r)
    return str(r)


async def run_case(text: str, engine: DecisionEngine, state: AgentState,
                   executor: AgentExecutor) -> dict:
    state.update_from_input(text)
    snap = state.snapshot()

    t0 = time.perf_counter()

    # Stage 1: decide
    decision = await engine.decide(text=text, state=snap, memories=[], messages=[])
    t_decide = (time.perf_counter() - t0) * 1000

    action = decision.action_type
    response = ""
    t_exec = 0.0

    if action == "tool":
        route = decision.content
        t1 = time.perf_counter()
        results = await executor.execute([route.to_executor_step()])
        t_exec = (time.perf_counter() - t1) * 1000
        response = _format(route.tool, results[0]) if results else ""

    elif action == "plan":
        steps = decision.content
        parts = []
        t1 = time.perf_counter()
        for route in steps:
            results = await executor.execute([route.to_executor_step()])
            parts.append(_format(route.tool, results[0]) if results else "")
        t_exec = (time.perf_counter() - t1) * 1000
        response = " ".join(parts)

    t_total = t_decide + t_exec
    return {
        "action":   action,
        "response": response,
        "t_decide": t_decide,
        "t_exec":   t_exec,
        "t_total":  t_total,
    }


async def run_tests():
    engine, state, executor = _make_components()

    # (text, expected_action, description)
    cases = [
        # Reflex tools — should be near-instant
        ("what time is it",         "tool",  "time query"),
        ("system info",             "tool",  "system info"),
        ("how's the CPU",           "tool",  "sysinfo alias"),
        ("open chrome",             "tool",  "launch app"),
        ("open vs code",            "tool",  "fuzzy app name"),
        ("open visual studio code", "tool",  "verbose app name"),
        ("open youtube",            "tool",  "open URL"),
        ("open new tube",           "tool",  "STT mishear -> youtube"),

        # Compound — plan
        ("open chrome and open notepad",   "plan", "compound: 2 apps"),
        ("open vs code and play spotify",  "plan", "compound: fuzzy + play verb"),

        # Speak (rule-based)
        ("thank you",               "speak", "gratitude"),

        # Ignore (low-signal)
        ("hmm",                     "ignore","low-signal"),

        # LLM fallback (no actual LLM call — just verify routing)
        ("explain neural networks", "llm",   "LLM fallback"),
    ]

    print(f"\n{'-'*72}")
    print("  JARVIS V2 -- Pipeline Integration Test")
    print(f"{'-'*72}")
    print(f"  {'INPUT':<35} {'EXPECT':<8} {'RESULT':<8} {'DECIDE':>7} {'EXEC':>7} {'TOTAL':>7}")
    print(f"{'-'*72}")

    passed = 0
    for text, expected, desc in cases:
        r = await run_case(text, engine, state, executor)
        ok = r["action"] == expected
        if ok:
            passed += 1
        tag  = PASS if ok else FAIL
        got  = r["action"]
        mark = f"  {DIM}{desc}{RST}" if ok else f"  <- got '{got}'"
        print(
            f"  {tag}  {text!r:<33} {expected:<8} {got:<8} "
            f"{r['t_decide']:>6.1f}ms {r['t_exec']:>6.1f}ms {r['t_total']:>6.1f}ms"
            f"{mark}"
        )

    total = len(cases)
    print(f"{'-'*72}")
    status = PASS if passed == total else FAIL
    print(f"  {status}  {passed}/{total} passed")
    print(f"{'-'*72}\n")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(run_tests())
