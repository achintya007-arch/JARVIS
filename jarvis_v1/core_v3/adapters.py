"""
core_v3/adapters.py — Thin bridges between existing modules and the V3 EventBus.

PerceptionAdapter — STT / hotword / keyboard         → UserInput events
ActionAdapter     — SpeakRequest events              → TTSEngine; direct tool dispatch
VaultAdapter      — StreamComplete events            → VaultMemory persistence (Phase B)
                  (Phase A: stub that logs and discards — Brain still runs end-to-end)
ResourceAdapter   — ResourceMonitor callbacks        → ResourceAlert events + model switching

PerceptionAdapter and ActionAdapter are ported verbatim from core_v2/adapters.py
(only the import roots change). VaultAdapter is new and replaces MemoryAdapter.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

from core_v3.event_bus import EventBus
from core_v3.events import (
    UserInput,
    SpeakRequest,
    StreamComplete,
    ResourceAlert,
    SystemEvent,
    PRIORITY_NORMAL,
    PRIORITY_IMPORTANT,
    PRIORITY_CRITICAL,
)
# Shared with V2 so bus-mediated speech (resource alerts, goal reminders) also
# suppresses STT input via the same guard object Brain uses — see its docstring.
from core_v2.adapters import SpeakingGuard

if TYPE_CHECKING:
    from core.config import Config
    from cognition.agent_state import AgentState
    from cognition.llm_client import LLMClient
    from infra.resource_monitor import ResourceMonitor
    from perception.stt import STTEngine
    from perception.tts import TTSEngine
    from perception.hotword import HotwordDetector
    from action.agent_executor import AgentExecutor
    from memory.vault_memory import VaultMemory

log = logging.getLogger("jarvis.v3.adapters")

_TTS_INTERRUPT_GRACE_SEC = 1.0


# ── PerceptionAdapter ─────────────────────────────────────────────────────────

# Wake-detection tuning: classify overlapping windows built from short reads.
# IDLE  : 0.5s reads x 3 = 1.5s window, ~2 checks/sec (accuracy, low GPU).
# SPEAK : 0.3s reads x 3 = 0.9s window, ~3 checks/sec (fast barge-in reaction).
_WAKE_READ_SECS   = 0.5
_BARGE_READ_SECS  = 0.3   # shorter reads while JARVIS talks -> react sooner
_WAKE_WINDOW_SEGS = 3


class PerceptionAdapter:
    """STT / hotword / keyboard → UserInput events."""

    def __init__(
        self,
        stt: "STTEngine",
        hotword: "HotwordDetector",
        bus: EventBus,
        config: "Config",
        speaking_guard=None,
        interrupt_cb=None,
    ) -> None:
        self._stt     = stt
        self._hotword = hotword
        self._bus     = bus
        self._config  = config
        # Shared "JARVIS is talking" flag — lets the wake loop know it should
        # barge-in (rather than start fresh) when the wake word lands mid-reply.
        self._speaking = speaking_guard
        # Called to cut off an in-flight reply on barge-in (Brain._interrupt_speech).
        self._interrupt_cb = interrupt_cb
        self._running = False
        self._loop_tasks: list[asyncio.Task] = []

        bus.subscribe(SystemEvent, self._on_system_event)

    async def _on_system_event(self, event: SystemEvent) -> None:
        if event.kind == "startup":
            self._running = True
            mode = getattr(self._config, "mode", "wake")

            if mode == "wake":
                self._loop_tasks.append(asyncio.create_task(self._wake_loop()))
            elif mode == "voice":
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
            for task in self._loop_tasks:
                task.cancel()
            if self._loop_tasks:
                await asyncio.gather(*self._loop_tasks, return_exceptions=True)
            self._loop_tasks.clear()

    # ── Wake mode (default) ─────────────────────────────────────────────────────
    # IDLE: only the cheap tiny.en detector runs on rolling windows from the
    # shared mic. base.en NEVER transcribes ambient audio. On "jarvis" -> chime
    # -> capture ONE command -> publish -> back to IDLE. Every input is therefore
    # intentional, and JARVIS never hears itself.
    #
    # WHILE SPEAKING: the loop keeps listening for the wake word ONLY (still
    # tiny.en, never base.en) so "Jarvis" spoken over a reply BARGES IN — cuts
    # the speech, then captures the new command. JARVIS's own output almost never
    # contains "jarvis", so speaker echo won't self-trigger it.

    async def _wake_loop(self) -> None:
        import numpy as np
        from collections import deque
        from perception.cues import play_chime

        keyword = getattr(self._hotword, "keyword", "jarvis")
        log.info("Wake mode active — listening for %r", keyword)
        print(f'[JARVIS] Wake mode -- say "{keyword}" to talk (Ctrl+C to exit)\n', flush=True)
        window: deque = deque(maxlen=_WAKE_WINDOW_SEGS)

        while self._running:
            try:
                # React faster while JARVIS is talking: shorter reads = a fresher,
                # tighter window and ~3 checks/sec instead of 2.
                speaking = self._speaking is not None and self._speaking.value
                read_secs = _BARGE_READ_SECS if speaking else _WAKE_READ_SECS

                seg = await asyncio.to_thread(self._stt.read_window, read_secs, 1.0)
                if seg is None:
                    continue

                window.append(seg)
                samples = np.concatenate(list(window))
                if not await asyncio.to_thread(self._hotword.detect, samples):
                    continue

                window.clear()
                if speaking:
                    # Barge-in: cut the current reply, then take the new command.
                    log.info("Barge-in (wake word during speech)")
                    if self._interrupt_cb is not None:
                        await self._interrupt_cb()
                await self._on_wake(play_chime)

            except (KeyboardInterrupt, EOFError):
                self._running = False
                break
            except Exception as e:
                log.error("Wake loop error: %s", e)

    async def _on_wake(self, chime) -> None:
        log.info("Wake word detected")
        print("\n[JARVIS] Listening...\n")
        await asyncio.to_thread(chime)
        # Discard the wake word + chime tail so the command capture starts clean.
        self._stt.flush()
        audio = await self._stt.record_utterance()
        text  = await self._stt.transcribe(audio)
        if text.strip():
            print(f"[Heard] {text}\n")
            self._bus.publish(UserInput(text=text, source="wake", timestamp=time.time()))

    async def _voice_loop(self) -> None:
        print("[JARVIS V3] Voice mode active (Ctrl+C to exit)\n")
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
        print("Jarvis V3 online. Type your message (Ctrl+C to exit).\n")
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
    """SpeakRequest → TTS, plus direct execute_tool() for Brain."""

    def __init__(
        self,
        tts: "TTSEngine",
        executor: "AgentExecutor",
        bus: EventBus,
        speaking_guard: "SpeakingGuard | None" = None,
    ) -> None:
        self._tts      = tts
        self._executor = executor
        # Shared with Brain — see SpeakingGuard docstring (core_v2/adapters.py).
        self._speaking = speaking_guard or SpeakingGuard()

        bus.subscribe(SpeakRequest, self._on_speak_request)

    async def _on_speak_request(self, event: SpeakRequest) -> None:
        if event.priority >= PRIORITY_CRITICAL:
            self._speaking.value = True
            try:
                try:
                    await asyncio.wait_for(self._tts.drain(), timeout=_TTS_INTERRUPT_GRACE_SEC)
                except asyncio.TimeoutError:
                    pass
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
        results = await self._executor.execute([{"tool": tool, "args": args}])
        return results[0] if results else {"tool": tool, "status": "error", "result": "No result"}

    async def drain(self) -> None:
        await self._tts.drain()

    async def stop_tts(self) -> None:
        try:
            await asyncio.wait_for(self._tts.drain(), timeout=0.1)
        except Exception:
            pass


# ── VaultAdapter (Phase A stub) ───────────────────────────────────────────────

class VaultAdapter:
    """
    Replaces V2's MemoryAdapter. Wraps a VaultMemory facade and bridges it to
    the event bus.

    Phase A: VaultMemory is None; this adapter is a no-op stub so the Brain
    pipeline runs end-to-end without persistence.
    Phase B: VaultMemory is wired in; build_messages and add_exchange become real.
    """

    def __init__(self, vault_memory: Optional["VaultMemory"], bus: EventBus) -> None:
        self._vault = vault_memory
        self._bus   = bus

        bus.subscribe(StreamComplete, self._on_stream_complete)

    async def _on_stream_complete(self, event: StreamComplete) -> None:
        if self._vault is None or not event.response:
            return
        try:
            await self._vault.add_exchange(event.original_text, event.response)
        except Exception as e:
            log.error("VaultAdapter persist error: %s", e)

    async def recall(self, text: str) -> list[str]:
        if self._vault is None or not self._vault.ready:
            return []
        try:
            return await self._vault.recall(text)
        except Exception:
            return []

    async def build_messages(self, text: str, intent: str | None = None) -> list[dict]:
        """
        Build LLM message list. Passes the conversation intent through so the
        memory layer can gate web-knowledge recall to factual questions.
        """
        if self._vault is None:
            return [{"role": "user", "content": text}]
        return await self._vault.build_messages(text, intent=intent)

    @property
    def ready(self) -> bool:
        return self._vault is not None and self._vault.ready


# ── ResourceAdapter ───────────────────────────────────────────────────────────

class ResourceAdapter:
    """Resource pressure callbacks → ResourceAlert events + model switching."""

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
