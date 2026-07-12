"""
core_v2/adapters.py — Thin bridges between existing modules and the EventBus.

Each adapter:
  - holds a reference to one or more existing modules (unchanged APIs)
  - subscribes to events it cares about
  - translates between events and existing module calls

No business logic lives here. All logic stays in Brain or the modules themselves.

Adapters:
  PerceptionAdapter  — STT / hotword / keyboard  → UserInput events
  ActionAdapter      — SpeakRequest events        → TTSEngine; direct tool dispatch
  MemoryAdapter      — StreamComplete events       → ContextManager persistence
  ResourceAdapter    — ResourceMonitor callbacks   → ResourceAlert events + model switching
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from core_v2.event_bus import EventBus
from core_v2.events import (
    UserInput,
    SpeakRequest,
    StreamComplete,
    ResourceAlert,
    SystemEvent,
    PRIORITY_NORMAL,
    PRIORITY_IMPORTANT,
    PRIORITY_CRITICAL,
)

if TYPE_CHECKING:
    from core.config import Config
    from cognition.agent_state import AgentState
    from cognition.llm_client import LLMClient
    from infra.resource_monitor import ResourceMonitor
    from perception.stt import STTEngine
    from perception.tts import TTSEngine
    from perception.hotword import HotwordDetector
    from cognition.context_manager import ContextManager
    from action.agent_executor import AgentExecutor

log = logging.getLogger("jarvis.adapters")

# When a CRITICAL SpeakRequest arrives, give TTS this long to finish its
# current sentence before cutting over. Avoids jarring mid-word clips.
_TTS_INTERRUPT_GRACE_SEC = 1.0


class SpeakingGuard:
    """
    Shared mutable "is JARVIS currently talking" flag.

    Brain's STT-suppression check and Brain-initiated speech (ack phrases, tool
    responses, LLM streaming) both go through Brain's own self._speaking.
    But SpeakRequest events published straight onto the bus at CRITICAL/
    IMPORTANT priority (resource alerts, goal reminders) are spoken directly by
    ActionAdapter, bypassing Brain entirely — so without a SHARED flag, STT
    keeps listening while JARVIS talks over those, and can hear (and act on)
    its own voice bleeding through the speakers. Brain and ActionAdapter both
    hold a reference to the same instance so either can set/read it.
    """
    def __init__(self) -> None:
        self.value: bool = False


# ── PerceptionAdapter ─────────────────────────────────────────────────────────

class PerceptionAdapter:
    """
    Bridges input sources (STT, hotword, keyboard) to UserInput events.

    Subscribes to SystemEvent("startup") to know when to begin listening.
    Subscribes to SystemEvent("shutdown") to stop loops cleanly.
    """

    def __init__(
        self,
        stt: "STTEngine",
        hotword: "HotwordDetector",
        bus: EventBus,
        config: "Config",
    ) -> None:
        self._stt     = stt
        self._hotword = hotword
        self._bus     = bus
        self._config  = config
        self._running = False
        # Fix #5: track loop tasks so they can be cancelled cleanly on shutdown.
        self._loop_tasks: list[asyncio.Task] = []

        bus.subscribe(SystemEvent, self._on_system_event)

    async def _on_system_event(self, event: SystemEvent) -> None:
        if event.kind == "startup":
            self._running = True
            mode = getattr(self._config, "mode", "text")

            # V2 has no wake state machine — the wake-word rebuild lives in V3.
            # Map "wake" to V2's always-listening voice loop so V2 still runs.
            if mode in ("voice", "wake"):
                self._loop_tasks.append(asyncio.create_task(self._voice_loop()))
            elif mode == "hotword" and self._config.hotword.enabled:
                self._loop_tasks.append(
                    asyncio.create_task(self._hotword.listen(callback=self._on_hotword))
                )
            else:
                self._loop_tasks.append(asyncio.create_task(self._text_loop()))

        elif event.kind == "shutdown":
            self._running = False
            self._hotword.stop()
            # Cancel and await all input loops so they don't outlive the process.
            for task in self._loop_tasks:
                task.cancel()
            if self._loop_tasks:
                await asyncio.gather(*self._loop_tasks, return_exceptions=True)
            self._loop_tasks.clear()

    async def _voice_loop(self) -> None:
        print("[JARVIS] Voice mode active (Ctrl+C to exit)\n")
        while self._running:
            try:
                audio = await self._stt.record_utterance()
                text  = await self._stt.transcribe(audio)
                if text.strip():
                    print(f"\n[Heard] {text}\n")
                    self._bus.publish(UserInput(text=text, source="stt", timestamp=time.time()))
            except (KeyboardInterrupt, EOFError):
                self._running = False
                break
            except Exception as e:
                log.error("Voice loop error: %s", e)

    async def _text_loop(self) -> None:
        print("Jarvis online. Type your message (Ctrl+C to exit).\n")
        while self._running:
            try:
                text = await asyncio.to_thread(input, "You: ")
                if text.strip():
                    self._bus.publish(UserInput(text=text, source="keyboard", timestamp=time.time()))
            except (KeyboardInterrupt, EOFError):
                self._running = False
                break

    async def _on_hotword(self) -> None:
        log.info("Wake word detected")
        audio = await self._stt.record_utterance()
        text  = await self._stt.transcribe(audio)
        if text.strip():
            self._bus.publish(UserInput(text=text, source="hotword", timestamp=time.time()))


# ── ActionAdapter ─────────────────────────────────────────────────────────────

class ActionAdapter:
    """
    Bridges SpeakRequest events to TTSEngine.
    Also exposes execute_tool() as a direct awaitable for Brain (request/response).

    TTS dispatch by priority:
      >= PRIORITY_CRITICAL  → interrupt (short grace period), then speak
      >= PRIORITY_IMPORTANT → enqueue + drain after current sentence
      == PRIORITY_NORMAL    → fire-and-forget enqueue
    """

    def __init__(
        self,
        tts: "TTSEngine",
        executor: "AgentExecutor",
        bus: EventBus,
        speaking_guard: "SpeakingGuard | None" = None,
    ) -> None:
        self._tts      = tts
        self._executor = executor
        # Shared with Brain so bus-mediated speech (resource alerts, goal
        # reminders) also suppresses STT input, not just Brain-initiated speech.
        self._speaking = speaking_guard or SpeakingGuard()

        bus.subscribe(SpeakRequest, self._on_speak_request)

    async def _on_speak_request(self, event: SpeakRequest) -> None:
        if event.priority >= PRIORITY_CRITICAL:
            self._speaking.value = True
            try:
                # Give current sentence a short window to finish, then cut over.
                try:
                    await asyncio.wait_for(
                        self._tts.drain(),
                        timeout=_TTS_INTERRUPT_GRACE_SEC,
                    )
                except asyncio.TimeoutError:
                    pass  # grace period expired — interrupt regardless
                await self._tts.speak(event.text)
            finally:
                self._speaking.value = False

        elif event.priority >= PRIORITY_IMPORTANT:
            self._speaking.value = True
            try:
                asyncio.create_task(self._tts.enqueue(event.text))
                await self._tts.drain()
            finally:
                self._speaking.value = False

        else:
            asyncio.create_task(self._tts.enqueue(event.text))

    async def execute_tool(self, tool: str, args: dict) -> dict:
        """
        Direct call — Brain awaits this to get tool results synchronously.
        ToolRequest/ToolResult events are published by Brain for observability.
        """
        results = await self._executor.execute([{"tool": tool, "args": args}])
        return results[0] if results else {"tool": tool, "status": "error", "result": "No result"}

    async def drain(self) -> None:
        """Wait for TTS queue to empty. Called by Brain after speak/tool paths."""
        await self._tts.drain()

    async def stop_tts(self) -> None:
        """Interrupt current speech immediately (called on user interrupt)."""
        try:
            await asyncio.wait_for(self._tts.drain(), timeout=0.1)
        except Exception:
            pass


# ── MemoryAdapter ─────────────────────────────────────────────────────────────

class MemoryAdapter:
    """
    Bridges StreamComplete events to ContextManager persistence.
    Also exposes recall() and build_messages() as direct awaitables for Brain
    (events can't return values, so these stay as direct calls).
    """

    def __init__(self, context: "ContextManager", bus: EventBus) -> None:
        self._context = context

        bus.subscribe(StreamComplete, self._on_stream_complete)

    async def _on_stream_complete(self, event: StreamComplete) -> None:
        if event.response:
            try:
                await self._context.add_exchange(event.original_text, event.response)
            except Exception as e:
                log.error("MemoryAdapter persist error: %s", e)

    async def recall(self, text: str) -> list[str]:
        """Semantic memory recall. Returns empty list if memory not ready."""
        if not self._context.memory_ready:
            return []
        try:
            return await self._context.memory_store_recall(text)
        except Exception:
            return []

    async def build_messages(self, text: str) -> list[dict]:
        """Build LLM message list with memory injection."""
        return await self._context.build_messages(text)

    @property
    def memory_ready(self) -> bool:
        return self._context.memory_ready


# ── ResourceAdapter ───────────────────────────────────────────────────────────

class ResourceAdapter:
    """
    Bridges ResourceMonitor pressure callbacks to ResourceAlert events,
    then handles those alerts: updates AgentState, switches LLM model,
    and publishes a SpeakRequest to alert the user.
    """

    def __init__(
        self,
        monitor: "ResourceMonitor",
        agent_state: "AgentState",
        llm: "LLMClient",
        config: "Config",
        bus: EventBus,
    ) -> None:
        self._monitor     = monitor
        self._agent_state = agent_state
        self._llm         = llm
        self._config      = config
        self._bus         = bus

        monitor.on_pressure(self._on_pressure)
        bus.subscribe(ResourceAlert, self._on_resource_alert)

    async def _on_pressure(self, metric: str, value: float, entering: bool) -> None:
        # Called only on a state TRANSITION (see ResourceMonitor._check_edge) —
        # never repeatedly while a metric stays elevated.
        snap = self._monitor.snapshot()
        self._bus.publish(ResourceAlert(metric=metric, value=value, snapshot=snap, entering=entering))

    async def _on_resource_alert(self, event: ResourceAlert) -> None:
        snap = event.snapshot
        self._agent_state.update_from_system(
            cpu_pct=snap.get("cpu_pct",  0.0),
            ram_pct=snap.get("ram_pct",  0.0),
            vram_pct=snap.get("vram_pct", 0.0),
        )

        before = self._llm.current_model
        target = self._config.llm.fallback_model if event.entering else self._config.llm.model
        await self._llm.set_model(target)
        switched = self._llm.current_model != before

        metric_label = "VRAM pressure" if event.metric == "vram_mb" else "RAM usage is critical"
        if event.entering:
            alert = (
                f"Sir, {metric_label}. Switching to the compact model."
                if switched else
                f"Sir, {metric_label}, but no lighter model is available to switch to."
            )
        else:
            alert = (
                "Pressure cleared. Restoring the standard model."
                if switched else
                "Pressure cleared, Sir."
            )

        log.warning(
            "Resource pressure %s: %s=%.1f -> %s",
            "entered" if event.entering else "cleared",
            event.metric, event.value, self._llm.current_model,
        )
        self._bus.publish(SpeakRequest(text=alert, priority=PRIORITY_CRITICAL))

    def snapshot(self) -> dict:
        return self._monitor.snapshot()
