"""
test_v2_resource.py — Resource pressure test (offline, no GPU/Ollama required).

Validates:
  1. VRAM pressure  → model switches to compact → SpeakRequest(CRITICAL)
  2. RAM pressure   → model switches to compact → SpeakRequest(CRITICAL)
  3. Pressure clear → model restored to default → SpeakRequest(CRITICAL)

No real ResourceMonitor polling — pressure callbacks fired manually.
"""

import asyncio
import sys
sys.path.insert(0, ".")

from core_v2.event_bus import EventBus
from core_v2.events import ResourceAlert, SpeakRequest, PRIORITY_CRITICAL
from core_v2.adapters import ResourceAdapter
from core.config import Config
from cognition.agent_state import AgentState
from cognition.llm_client import LLMClient

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


# ── Minimal stubs ─────────────────────────────────────────────────────────────

class MockMonitor:
    def __init__(self):
        self._callbacks = []
        self._snap = {"cpu_pct": 10.0, "ram_pct": 50.0, "vram_pct": 60.0,
                      "vram_mb": 5000.0}

    def on_pressure(self, cb):
        self._callbacks.append(cb)

    def snapshot(self):
        return self._snap.copy()

    async def fire(self, metric: str, value: float, snap_override: dict = None):
        if snap_override:
            self._snap.update(snap_override)
        for cb in self._callbacks:
            await cb(metric, value)


# ── Test runner ───────────────────────────────────────────────────────────────

async def run_tests():
    config  = Config()
    default_model  = config.llm.model
    compact_model  = "qwen2.5:3b-instruct-q4_K_M"

    results = []

    async def run_case(label: str, fire_metric: str, fire_value: float,
                       snap: dict, expect_model: str, expect_priority: int):
        bus     = EventBus()
        monitor = MockMonitor()
        state   = AgentState()
        llm     = LLMClient(config.llm)

        spoken = []
        async def capture_speak(event: SpeakRequest):
            spoken.append(event)
        bus.subscribe(SpeakRequest, capture_speak)

        ResourceAdapter(monitor, state, llm, config, bus)

        await monitor.fire(fire_metric, fire_value, snap)
        await asyncio.sleep(0.05)   # let bus tasks settle

        model_ok    = llm.current_model == expect_model
        spoke       = len(spoken) > 0
        priority_ok = spoke and spoken[0].priority == expect_priority

        ok = model_ok and spoke and priority_ok
        results.append((ok, label, llm.current_model, expect_model,
                        spoken[0].text if spoke else "<silent>"))

    await run_case(
        label          = "VRAM pressure (>6500 MB)",
        fire_metric    = "vram_mb",
        fire_value     = 7000.0,
        snap           = {"cpu_pct": 20.0, "ram_pct": 55.0, "vram_pct": 92.0, "vram_mb": 7000.0},
        expect_model   = compact_model,
        expect_priority= PRIORITY_CRITICAL,
    )

    await run_case(
        label          = "RAM pressure (>85%)",
        fire_metric    = "ram_pct",
        fire_value     = 90.0,
        snap           = {"cpu_pct": 30.0, "ram_pct": 90.0, "vram_pct": 50.0, "vram_mb": 4000.0},
        expect_model   = compact_model,
        expect_priority= PRIORITY_CRITICAL,
    )

    await run_case(
        label          = "Pressure cleared",
        fire_metric    = "cpu_pct",   # non-pressure metric → else branch
        fire_value     = 15.0,
        snap           = {"cpu_pct": 15.0, "ram_pct": 40.0, "vram_pct": 30.0, "vram_mb": 3000.0},
        expect_model   = default_model,
        expect_priority= PRIORITY_CRITICAL,
    )

    print(f"\n{'-'*65}")
    print("  JARVIS V2 -- Resource Pressure Test")
    print(f"{'-'*65}")

    passed = 0
    for ok, label, got_model, exp_model, spoken_text in results:
        tag = PASS if ok else FAIL
        if ok:
            passed += 1
        print(f"  {tag}  {label}")
        print(f"        model : {got_model}")
        print(f"        spoke : {spoken_text[:70]}")
        if not ok:
            print(f"        EXPECT: {exp_model}")

    total = len(results)
    print(f"{'-'*65}")
    status = PASS if passed == total else FAIL
    print(f"  {status}  {passed}/{total} passed")
    print(f"{'-'*65}\n")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(run_tests())
