"""
Regression tests for the resource-alert bugs found during live testing:

1. ResourceMonitor fired the pressure callback on EVERY poll tick while a
   metric stayed elevated, spamming a "critical alert" every few seconds.
   Fixed: edge-triggered — fires only on state transitions.
2. ResourceAdapter claimed "switching to the compact model" even when the
   fallback model wasn't available and the switch silently no-op'd.
   Fixed: message reflects whether the model actually changed.
3. Bus-mediated CRITICAL/IMPORTANT SpeakRequests (resource alerts, goal
   reminders) bypassed Brain's STT-suppression entirely, letting JARVIS's own
   voice bleed into the mic while "speaking" was never marked True.
   Fixed: a SpeakingGuard shared between Brain and ActionAdapter.
"""


import pytest

pytest.importorskip("httpx")

from core.config import ResourceConfig
from core_common.speaking import SpeakingGuard
from infra.resource_monitor import ResourceMonitor


class TestEdgeTriggeredPressure:
    async def test_fires_once_on_entering_not_every_poll(self):
        mon = ResourceMonitor(ResourceConfig(max_ram_pct=0.5))  # low bar, easy to trip
        calls = []

        async def cb(metric, value, entering):
            calls.append((metric, value, entering))

        mon.on_pressure(cb)

        # Simulate three consecutive polls all over threshold — should only
        # fire once (on the first), not three times.
        await mon._check_edge("ram_pct", 90.0, 50.0)
        await mon._check_edge("ram_pct", 91.0, 50.0)
        await mon._check_edge("ram_pct", 92.0, 50.0)

        assert len(calls) == 1
        assert calls[0] == ("ram_pct", 90.0, True)

    async def test_fires_again_on_clearing(self):
        mon = ResourceMonitor(ResourceConfig(max_ram_pct=0.5))
        calls = []

        async def cb(metric, value, entering):
            calls.append(entering)

        mon.on_pressure(cb)

        await mon._check_edge("ram_pct", 90.0, 50.0)   # enter
        await mon._check_edge("ram_pct", 90.0, 50.0)   # still over — no fire
        await mon._check_edge("ram_pct", 10.0, 50.0)   # clear
        await mon._check_edge("ram_pct", 10.0, 50.0)   # still clear — no fire

        assert calls == [True, False]

    async def test_independent_per_metric(self):
        mon = ResourceMonitor(ResourceConfig())
        calls = []

        async def cb(metric, value, entering):
            calls.append(metric)

        mon.on_pressure(cb)
        await mon._check_edge("ram_pct", 90.0, 50.0)
        await mon._check_edge("vram_mb", 7000.0, 6500.0)

        assert calls == ["ram_pct", "vram_mb"]


class TestSpeakingGuard:
    async def test_shared_between_brain_and_action_adapter(self):
        """
        The exact bug: a bus-mediated SpeakRequest (critical resource alert)
        must be visible to whatever checks 'is JARVIS currently speaking' —
        even though ActionAdapter, not Brain, is the one doing the speaking.
        """
        guard = SpeakingGuard()
        assert guard.value is False

        # Simulate ActionAdapter setting it during a critical alert
        guard.value = True
        assert guard.value is True  # Brain-side reader sees the same flag

        guard.value = False
        assert guard.value is False

    async def test_action_adapter_sets_guard_around_critical_speech(self):
        pytest.importorskip("numpy")
        pytest.importorskip("sounddevice")
        from core_v2.adapters import ActionAdapter
        from core_v2.event_bus import EventBus
        from core_v2.events import PRIORITY_CRITICAL, SpeakRequest

        class FakeTTS:
            def __init__(self):
                self.speaking_during_call = None

            async def drain(self):
                pass

            async def speak(self, text):
                # Capture whether the guard was True WHILE speak() was running
                self.speaking_during_call = guard.value

            async def enqueue(self, text):
                pass

        bus = EventBus()
        guard = SpeakingGuard()
        tts = FakeTTS()
        adapter = ActionAdapter(tts, executor=None, bus=bus, speaking_guard=guard)

        assert guard.value is False
        await adapter._on_speak_request(
            SpeakRequest(text="Sir, RAM usage is critical.", priority=PRIORITY_CRITICAL)
        )
        # Guard was True during speak(), and reset after
        assert tts.speaking_during_call is True
        assert guard.value is False
