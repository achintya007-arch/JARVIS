"""
core_v3/brain.py — V3 composition root.

After the voice rebuild, Brain contains NO voice logic. It wires together:
  - cognition (unchanged): LLMClient, AgentState, FastRouter, DecisionEngine,
    GoalManager, VaultMemory, AgentExecutor, AgentCore
  - the ConversationEngine (text → response sentences)
  - the voice layer (MicrophoneInput, WakeWordDetector, EndpointDetector,
    STTEngine, TTSEngine, AudioPlayer, InterruptionController, VoiceOrchestrator)

Run with: python -m core_v3   (voice/wake mode)   ·   JARVIS_MODE=text for keyboard.
"""

from __future__ import annotations

import asyncio
import logging

from action.agent_executor import AgentExecutor
from action.fast_router import FastRouter
from action.obsidian_tools import ObsidianTools
from cognition.agent_core import AgentCore
from cognition.agent_state import AgentState
from cognition.decision_engine import DecisionEngine
from cognition.goal_manager import GoalManager
from cognition.llm_client import LLMClient
from core.config import Config
from core_v3.adapters import ResourceAdapter
from core_v3.conversation import ConversationEngine
from core_v3.event_bus import EventBus
from core_v3.events import SpeakRequest, SystemEvent
from core_v3.voice.orchestrator import VoiceOrchestrator
from core_v3.voice.state import VoiceState
from infra.resource_monitor import ResourceMonitor
from memory.vault_memory import VaultMemory
from perception.audio_player import AudioPlayer
from perception.endpointer import EndpointDetector
from perception.microphone import MicrophoneInput
from perception.stt import STTEngine
from perception.tts import TTSEngine
from perception.wakeword import WakeWordDetector

log = logging.getLogger("jarvis.v3.brain")


class Brain:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._bus = EventBus()
        self._stopped = False
        self._stop_event = asyncio.Event()
        self._orchestrator: VoiceOrchestrator | None = None

        # ── Infrastructure / cognition (unchanged) ─────────────────────────
        self._resource_monitor = ResourceMonitor(config.resources)
        self._llm = LLMClient(config.llm)
        self._agent_state = AgentState()
        self._fast_router = FastRouter()
        self._decision_engine = DecisionEngine(fast_router=self._fast_router)
        self._vault_memory = VaultMemory(
            config.vault, ollama_url=config.llm.base_url, web_rag_config=config.web_rag,
        )
        self._obsidian = ObsidianTools(self._vault_memory)
        self._goal_manager = GoalManager(
            db_path=None, agent_state=self._agent_state, bus=self._bus,
            vault=self._vault_memory.vault,
        )
        self._goal_task: asyncio.Task | None = None

        # Executor's speak/confirm are late-bound to the orchestrator (built below).
        self._executor = AgentExecutor(
            config=config.agent,
            speak_callback=self._speak_cb,
            confirm_callback=self._confirm_cb,
            vault_tools=self._obsidian,
        )
        self._agent_core = AgentCore(
            llm=self._llm, executor=self._executor,
            max_turns=getattr(config, "agent_core_max_turns", 3),
            stream_to_stdout=False,
        )
        self._conversation = ConversationEngine(
            config=config, llm=self._llm, agent_state=self._agent_state,
            decision_engine=self._decision_engine, goal_manager=self._goal_manager,
            vault_memory=self._vault_memory, executor=self._executor,
            agent_core=self._agent_core, fast_router=self._fast_router,
        )

        # ── Voice layer (rebuilt) ──────────────────────────────────────────
        frame_ms = config.voice.frame_ms
        self._mic = MicrophoneInput(frame_ms=frame_ms)
        self._wake = WakeWordDetector(config.wake)
        self._endpointer = EndpointDetector(config.stt, frame_ms=frame_ms)
        self._stt = STTEngine(config.stt)
        self._tts = TTSEngine(config.tts)
        self._player = AudioPlayer()
        from core_v3.voice.interruption import InterruptionController
        self._interruption = InterruptionController(config.voice, config.stt, frame_ms=frame_ms)

        self._orchestrator = VoiceOrchestrator(
            mic=self._mic, wake=self._wake, endpointer=self._endpointer,
            stt=self._stt, tts=self._tts, player=self._player,
            interruption=self._interruption, conversation=self._conversation,
            voice_config=config.voice, resource_snapshot=self._resource_monitor.snapshot,
        )

        # Resource pressure → model switching + spoken alerts (via orchestrator).
        self._resource = ResourceAdapter(
            self._resource_monitor, self._agent_state, self._llm, config, self._bus,
        )
        self._bus.subscribe(SpeakRequest, self._on_speak_request)

    # Late-bound executor callbacks → orchestrator.
    async def _speak_cb(self, text: str) -> None:
        if self._orchestrator is not None:
            await self._orchestrator.speak(text)

    async def _confirm_cb(self, prompt: str) -> bool:
        if self._orchestrator is None:
            return False
        return await self._orchestrator.confirm(prompt)

    async def _on_speak_request(self, event: SpeakRequest) -> None:
        # Resource alerts etc. — speak only when idle so we never talk over a turn.
        if self._orchestrator is not None and self._orchestrator.state.is_(VoiceState.IDLE):
            await self._orchestrator.speak(event.text)

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    async def start(self) -> None:
        log.info("Brain V3 starting...")
        await asyncio.gather(
            self._resource_monitor.start(),
            self._vault_memory.initialize(),
            self._tts.initialize(),
        )
        await self._goal_manager.initialize()
        self._goal_task = asyncio.create_task(self._goal_manager.run())
        self._bus.publish(SystemEvent(kind="startup"))

        mode = getattr(self.config, "mode", "wake")
        try:
            if mode == "text":
                await self._text_loop()
            else:
                await self._orchestrator.start()
        finally:
            await asyncio.shield(self.stop())

    async def _text_loop(self) -> None:
        print("JARVIS V3 (text mode). Type a message, Ctrl+C to exit.\n", flush=True)
        while not self._stopped:
            try:
                text = await asyncio.to_thread(input, "You: ")
            except (EOFError, KeyboardInterrupt):
                break
            if not text.strip():
                continue
            async for sentence in self._conversation.respond(text):
                print(f"Echo: {sentence}", flush=True)

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        log.info("Brain V3 shutting down...")
        if self._goal_task:
            self._goal_task.cancel()
        # Lazily create each teardown coroutine only when awaited — building them
        # all upfront left later ones un-awaited (RuntimeWarning) if an earlier
        # step was cancelled during shutdown.
        steps = [
            ("orchestrator",     self._orchestrator.stop if self._orchestrator else None),
            ("goal_manager",     self._goal_manager.close),
            ("resource_monitor", self._resource_monitor.stop),
            ("executor",         self._executor.shutdown),
            ("tts",              self._tts.shutdown),
            ("vault_memory",     self._vault_memory.close),
        ]
        for label, make in steps:
            if make is None:
                continue
            try:
                await make()
            except Exception as e:
                log.warning("shutdown step '%s' failed: %s", label, e)
        self._stop_event.set()
        log.info("Brain V3 shutdown complete.")
