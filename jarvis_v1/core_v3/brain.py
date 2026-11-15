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
from core_v3.adapters.bbp_commands import BBPCommands
from core_v3.adapters.bbp_server import BBPServer
from core_v3.adapters.phonebox import PhoneBoxAdapter
from core_v3.adapters.remote_desktop import RemoteDesktop
from core_v3.confirmations import ConfirmationBroker
from core_v3.console import ConsoleInput
from core_v3.conversation import TURN_FAILED_REPLY, ConversationEngine
from core_v3.event_bus import EventBus
from core_v3.events import SpeakRequest, SystemEvent, TimerFinished
from core_v3.voice.orchestrator import _AFFIRMATIVE_RE, NO_CONFIRMATION_HEARD, VoiceOrchestrator
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
        self._turn_lock = asyncio.Lock()     # text mode: one turn at a time (stdin or Body)
        self._orchestrator: VoiceOrchestrator | None = None
        self._console = ConsoleInput()       # text mode: chat loop + confirmations share stdin
        # Every pending confirmation: voice, console and Body dialogs race to answer.
        self._confirmations = ConfirmationBroker(self._bus, timeout_s=config.bbp.confirm_timeout_s)

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
            confirm_request_callback=self._confirm_request,
            timer_done_callback=lambda label: self._bus.publish(TimerFinished(label=label)),
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
            bus=self._bus,
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
            bus=self._bus,
        )

        # Resource pressure → model switching + spoken alerts (via orchestrator).
        self._resource = ResourceAdapter(
            self._resource_monitor, self._agent_state, self._llm, config, self._bus,
        )
        self._bus.subscribe(SpeakRequest, self._on_speak_request)

        # Brain–Body Protocol server (C# shell / probe). Optional and isolated:
        # created only when enabled; voice and text mode never depend on it.
        self._bbp: BBPServer | None = None
        if config.bbp.enabled:
            self._bbp = BBPServer(config.bbp, self._bus, self._bbp_snapshot)
            BBPCommands(
                orchestrator=self._orchestrator, conversation=self._conversation,
                config=config.bbp, turn_lock=self._turn_lock,
                voice_active=lambda: getattr(self.config, "mode", "wake") != "text",
                confirmations=self._confirmations,
            ).register(self._bbp)
            # Desktop tools go to a connected Body when it can do them; otherwise
            # the Python tools run (unless bbp.desktop_fallback is false).
            self._executor.attach_remote_desktop(
                RemoteDesktop(self._bbp, timeout_s=config.bbp.request_timeout_s),
                fallback=config.bbp.desktop_fallback,
            )

        # PhoneBox desk panel (a phone showing JARVIS's state). Optional and
        # isolated like BBP: a bus observer that posts to the local PhoneBox agent.
        self._phonebox: PhoneBoxAdapter | None = None
        if config.phonebox.enabled:
            self._phonebox = PhoneBoxAdapter(
                config.phonebox, self._bus, current_model=lambda: self._llm.current_model,
                timers=self._executor.active_timers, goals=self._active_goals,
            )

    # Late-bound executor callbacks → orchestrator.
    async def _speak_cb(self, text: str) -> None:
        if self._orchestrator is not None:
            await self._orchestrator.speak(text)

    async def _confirm_cb(self, prompt: str) -> bool:
        if self._orchestrator is None:
            return False
        return await self._orchestrator.confirm(prompt)

    # ── Confirmations (BBP milestone 3) ───────────────────────────────────────
    def _text_mode(self) -> bool:
        return getattr(self.config, "mode", "wake") == "text"

    async def _confirm_request(self, request):
        """A gated tool asks. The broker races the Body dialogs against your
        voice (voice mode, unless the mic is muted) or the console (text mode)."""
        text_mode = self._text_mode()
        channels = {}
        if text_mode:
            channels["text"] = self._console_answer
        elif self._orchestrator is not None and not self._orchestrator.muted:
            channels["voice"] = self._orchestrator_answer
        result = await self._confirmations.ask(request, channels)
        if result.by == "timeout":
            if text_mode:
                print("\nJARVIS: No answer in time, so I won't do it.", flush=True)
            elif self._orchestrator is not None:
                await self._orchestrator.speak(NO_CONFIRMATION_HEARD)
        elif result.by == "body" and text_mode:
            verdict = "approved" if result.approved else "denied"
            print(f"\n(answered on the HUD: {verdict})", flush=True)
        elif result.by == "error" and text_mode:
            print("\nJARVIS: I couldn't get a clear answer (the HUD may have closed), so I won't do it.",
                  flush=True)
        return result               # Resolution: approved, by, confirm_id

    async def _orchestrator_answer(self, request, timeout: float) -> bool | None:
        return await self._orchestrator.listen_for_confirmation(request.prompt, timeout)

    async def _console_answer(self, request, timeout: float) -> bool | None:
        print(f"JARVIS: {request.prompt}", flush=True)
        if request.detail:
            print(f"        > {request.detail}", flush=True)
        if request.action and "text" in request.action:
            print("        text to type:", flush=True)
            for line in request.action["text"].split("\n"):
                print(f"        | {line}", flush=True)
        while True:
            line = (await self._console.readline(f"Approve? yes/no ({timeout:.0f} s): ")).strip()
            if line:
                return bool(_AFFIRMATIVE_RE.match(line))

    async def _on_speak_request(self, event: SpeakRequest) -> None:
        # Resource alerts etc. — speak only when idle so we never talk over a turn.
        if self._orchestrator is not None and self._orchestrator.state.is_(VoiceState.IDLE):
            await self._orchestrator.speak(event.text)

    def _active_goals(self) -> list[dict]:
        return [
            {"name": g.name, "priority": int(g.priority)}
            for g in self._agent_state.active_goals if g.is_active
        ]

    def _bbp_snapshot(self) -> dict:
        """State a Body needs to redraw instantly (BBP welcome.snapshot)."""
        orch = self._orchestrator
        voice = {
            "state": orch.state.state.value.lower() if orch else "idle",
            "muted": orch.muted if orch else False,
        }
        snap = self._agent_state.snapshot()
        res = self._resource_monitor.snapshot()
        agent = {
            "mood": snap.user_state,
            "intent": snap.conversation_intent,
            "model": self._llm.current_model,
            "load": {"ram_pct": res.get("ram_pct"), "vram_pct": res.get("vram_pct")},
        }
        return {
            "voice": voice,
            "agent": agent,
            "active_goals": self._active_goals(),
            "pending_confirmations": self._confirmations.pending(),
        }

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
        if self._bbp is not None:
            await self._bbp.start()          # logs and carries on if it can't bind
        if self._phonebox is not None:
            await self._phonebox.start()     # never fails; PhoneBox may start later
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
        asked_question = False
        while not self._stopped:
            try:
                text = await self._console.readline("You: ")
            except (EOFError, KeyboardInterrupt):
                break
            if not text.strip():
                continue
            last = ""
            # If JARVIS's previous reply was a question, a short answer ("yes")
            # is a reply to it, not low-signal noise.
            try:
                async with self._turn_lock:  # a Body's input.text waits its turn (E_BUSY)
                    async for sentence in self._conversation.respond(text, followup=asked_question):
                        last = sentence
                        print(f"JARVIS: {sentence}", flush=True)
            except Exception as e:      # a failed turn must not end the session
                log.error("text turn failed: %s", e, exc_info=True)
                last = TURN_FAILED_REPLY
                print(f"JARVIS: {TURN_FAILED_REPLY}", flush=True)
            asked_question = last.rstrip().endswith("?")

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
            ("bbp",              self._bbp.stop if self._bbp else None),
            ("phonebox",         self._phonebox.stop if self._phonebox else None),
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
