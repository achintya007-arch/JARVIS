"""
core_v3/brain.py — V3 orchestrator (Phase A skeleton).

Phase A goal: prove the V3 pipeline runs end-to-end:
    UserInput → AgentState update → FastRouter → (Tool | LLM stream) → TTS

Memory is a no-op stub (VaultAdapter wraps None VaultMemory). Phase B will
wire in the real VaultMemory. Goals, proactive loop, and Obsidian tools land
in Phase C. Web RAG lands in Phase D.

Diff from core_v2/brain.py:
  - No ContextManager / MemoryAdapter — replaced by stub VaultAdapter
  - No GoalManager (yet — Phase C)
  - No internal proactive loop (yet — would re-enable in Phase C)
  - System prompt context lines from active vault notes (Phase B onward)
  - Everything else (suggestion confirmation, _speaking flag, decision pipeline,
    LLM streaming, action enforcement) ports verbatim.

V2 stays runnable at `python main.py`. V3 runs via `python -m core_v3`.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from datetime import datetime

from action.agent_executor import AgentExecutor
from action.fast_router import FastRouter
from action.obsidian_tools import ObsidianTools
from cognition.agent_core import AgentCore
from cognition.agent_core.planner import TOOL_SCHEMAS
from cognition.agent_state import AgentState
from cognition.decision_engine import (
    PRIORITY_HIGH,
    Decision,
    DecisionEngine,
)
from cognition.goal_manager import GoalManager
from cognition.llm_client import LLMClient, build_system_prompt
from core.config import Config
from core_v3.adapters import (
    ActionAdapter,
    PerceptionAdapter,
    ResourceAdapter,
    SpeakingGuard,
    VaultAdapter,
)
from core_v3.event_bus import EventBus
from core_v3.events import (
    ActionSuggestion,
    DecisionReady,
    StateUpdated,
    StreamComplete,
    SystemEvent,
    ToolRequest,
    ToolResult,
    UserInput,
)
from infra.resource_monitor import ResourceMonitor
from memory.vault_memory import VaultMemory
from perception.hotword import HotwordDetector
from perception.stt import STTEngine
from perception.tts import TTSEngine

log = logging.getLogger("jarvis.v3.brain")

_ACK_PHRASES = [
    "Yes, Sir.",
    "Right away.",
    "One moment, Sir.",
    "Understood.",
    "On it.",
]

# How often the proactive loop wakes to consider volunteering something.
# The thought itself is heavily gated (idle, system-normal, low probability),
# so a short tick doesn't mean frequent speech.
_INTERNAL_LOOP_INTERVAL = 5  # seconds

# ── Suggestion confirmation (ported from V2; used in Phase C onward) ──────────
_SUGGESTION_TTL_SEC = 30

_AFFIRMATIVE_RE = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|sure|ok|okay|please|do it|go ahead|affirmative)\b",
    re.IGNORECASE,
)
_NEGATIVE_RE = re.compile(
    r"^\s*(?:no|nope|nah|negative|cancel|skip|nevermind|never mind|don'?t)\b",
    re.IGNORECASE,
)

# Barge-in — phrases that, spoken WHILE JARVIS is talking, interrupt the speech.
# Deliberately words JARVIS rarely says itself, so speaker echo won't self-trigger.
_INTERRUPT_RE = re.compile(
    r"\b(?:stop|wait|hold on|cancel|nevermind|never mind|quiet|shut up|enough|jarvis)\b",
    re.IGNORECASE,
)
# A bare stop command (just halt) vs. a barge-in that carries a new instruction.
_STOP_ONLY_RE = re.compile(
    r"^\s*(?:stop|wait|hold on|quiet|shut up|enough|cancel|nevermind|never mind)"
    r"[\s.!,]*(?:jarvis)?[\s.!,]*$",
    re.IGNORECASE,
)

_STRIP_RE = re.compile(
    r"```.*?```|`[^`]*`|[*_#~>|\\]|\[([^\]]+)\]\([^)]+\)",
    re.DOTALL,
)


def _clean(text: str) -> str:
    text = _STRIP_RE.sub(r"\1", text)
    text = re.sub(r"\n{2,}", " ", text)
    return re.sub(r" {2,}", " ", text).strip()


def _time_of_day(now: datetime) -> str:
    h = now.hour
    if h < 12:  return "morning"
    if h < 17:  return "afternoon"
    return "evening"


def _format_fast_result(tool: str, result: dict) -> str:
    status = result.get("status")
    if status == "denied":
        return f"That action is restricted, Sir. {result.get('reason', '')}".strip()
    if status == "error":
        return f"I encountered an error with that, Sir. {result.get('error', '')}".strip()

    r = result.get("result", "")
    if tool == "get_time":    return f"It is {r}, Sir."
    if tool == "system_info": return r
    if tool == "weather":     return r
    if tool == "set_timer":   return r
    return str(r)


class Brain:
    """
    V3 orchestrator. Replaces core_v2.brain.Brain.

    Same external interface: Brain(config) → await brain.start().
    Phase A wiring: perception → decision → action; no memory persistence.
    """

    def __init__(self, config: Config) -> None:
        self.config   = config
        self._bus     = EventBus()
        self._running = False
        self._stopped = False
        self._stop_event = asyncio.Event()
        self._active_task: asyncio.Task | None = None
        self._internal_loop_task: asyncio.Task | None = None
        # Shared with ActionAdapter (see SpeakingGuard docstring in
        # core_v2/adapters.py) so bus-mediated speech — resource alerts, goal
        # reminders — also suppresses STT, not just Brain-initiated speech.
        self._speaking_guard = SpeakingGuard()

        # ── Infrastructure ────────────────────────────────────────────────
        self._resource_monitor = ResourceMonitor(config.resources)
        self._stt     = STTEngine(config.stt)
        self._tts     = TTSEngine(config.tts)
        self._hotword = HotwordDetector(config.hotword)

        # ── Cognition ─────────────────────────────────────────────────────
        self._llm = LLMClient(config.llm)
        self._agent_state    = AgentState()
        self._fast_router    = FastRouter()
        self._decision_engine = DecisionEngine(fast_router=self._fast_router)

        # ── Memory (built before Action so the executor gets vault tools) ──
        # Phase B: real vault-backed memory. Phase C: vault tools for the agent.
        self._vault_memory = VaultMemory(
            config.vault,
            ollama_url=config.llm.base_url,
            web_rag_config=config.web_rag,
        )
        self._obsidian     = ObsidianTools(self._vault_memory)

        # Phase C: goals persist as Obsidian tasks in the vault's goals.md.
        self._goal_manager = GoalManager(
            db_path     = None,
            agent_state = self._agent_state,
            bus         = self._bus,
            vault       = self._vault_memory.vault,
        )
        self._goal_manager_task: asyncio.Task | None = None

        # ── Action ────────────────────────────────────────────────────────
        self._executor = AgentExecutor(
            config           = config.agent,
            speak_callback   = self._tts_speak,
            confirm_callback = self._confirm_action,
            vault_tools      = self._obsidian,
        )
        self._agent_core = AgentCore(
            llm              = self._llm,
            executor         = self._executor,
            max_turns        = getattr(config, "agent_core_max_turns", 3),
            stream_to_stdout = True,
        )

        # ── Adapters ──────────────────────────────────────────────────────
        self._perception = PerceptionAdapter(
            self._stt, self._hotword, self._bus, config,
            speaking_guard=self._speaking_guard,
            interrupt_cb=self._interrupt_speech,
        )
        self._action     = ActionAdapter(self._tts, self._executor, self._bus, self._speaking_guard)
        self._vault      = VaultAdapter(vault_memory=self._vault_memory, bus=self._bus)
        self._resource   = ResourceAdapter(
            self._resource_monitor, self._agent_state, self._llm, config, self._bus
        )

        # Brain subscribes to user input + suggestions
        self._bus.subscribe(UserInput, self._handle_user_input)
        self._bus.subscribe(ActionSuggestion, self._handle_action_suggestion)

        # Suggestion slot (used in Phase C when GoalManager fires)
        self._pending_suggestion: ActionSuggestion | None = None

        # Confirmation slot — a future resolved by the next user utterance
        # (voice or text) when the executor asks to confirm a gated action.
        self._pending_confirm: asyncio.Future | None = None

    # `_speaking` reads/writes the shared guard so every existing call site
    # (self._speaking = True/False, if self._speaking) keeps working unchanged
    # while ActionAdapter observes/sets the SAME flag for bus-mediated speech.
    @property
    def _speaking(self) -> bool:
        return self._speaking_guard.value

    @_speaking.setter
    def _speaking(self, value: bool) -> None:
        self._speaking_guard.value = value

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        log.info("Brain V3 (Phase B) initialising...")

        await asyncio.gather(
            self._resource_monitor.start(),
            self._stt.initialize(),
            self._tts.initialize(),
            self._vault_memory.initialize(),
        )

        # Goal manager needs the vault initialized first (reads goals.md).
        await self._goal_manager.initialize()
        self._goal_manager_task = asyncio.create_task(self._goal_manager.run())

        # Ambient presence — proactive volunteering when idle.
        self._internal_loop_task = asyncio.create_task(self._internal_loop())

        await self._greet()

        await self._bus.publish_and_wait(SystemEvent(kind="startup"))

        # Block until stopped. On Ctrl+C the wait is cancelled; the finally
        # guarantees graceful teardown runs (audit H3 — stop() was dead code).
        try:
            await self._stop_event.wait()
        finally:
            await asyncio.shield(self.stop())

    async def stop(self) -> None:
        """Idempotent, defensive teardown. Safe to call from a cancelled task."""
        if self._stopped:
            return
        self._stopped = True
        self._running = False
        log.info("Brain V3 shutting down...")

        if self._goal_manager_task:
            self._goal_manager_task.cancel()
        if self._internal_loop_task:
            self._internal_loop_task.cancel()

        self._bus.publish(SystemEvent(kind="shutdown"))
        for label, coro in (
            ("goal_manager",     self._goal_manager.close()),
            ("resource_monitor", self._resource_monitor.stop()),
            ("executor",         self._executor.shutdown()),
            ("vault_memory",     self._vault_memory.close()),
            ("tts",              self._tts.shutdown()),
        ):
            try:
                await coro
            except Exception as e:
                log.warning("Shutdown step '%s' failed: %s", label, e)

        self._stop_event.set()
        log.info("Brain V3 shutdown complete.")

    # ── Greeting ──────────────────────────────────────────────────────────────

    async def _greet(self) -> None:
        now  = datetime.now()
        snap = self._resource.snapshot()
        cpu  = snap.get("cpu_pct", 0)
        ram  = snap.get("ram_pct", 0)
        vram = f" VRAM at {snap['vram_pct']:.0f} percent." if "vram_pct" in snap else ""

        mem = " Vault memory is online." if self._vault_memory.recall_ready else ""
        greeting = (
            f"Good {_time_of_day(now)}, Sir. All systems are online. "
            f"It is {now.strftime('%I:%M %p').lstrip('0')} on {now.strftime('%A, %B %d')}. "
            f"CPU at {cpu:.0f} percent, RAM at {ram:.0f} percent.{vram}{mem}"
        )
        log.info("Greeting: %s", greeting)
        self._speaking = True
        try:
            await self._tts.speak(greeting)
        finally:
            self._speaking = False

    # ── Suggestion confirmation (GoalManager proposes actionable goals) ───────

    async def _handle_action_suggestion(self, event: ActionSuggestion) -> None:
        self._pending_suggestion = event
        log.info("Suggestion pending: %r", event.action_text)
        self._speaking = True
        try:
            await self._tts.speak(event.speak_text)
        finally:
            self._speaking = False

    def _consume_pending_suggestion(self) -> ActionSuggestion | None:
        pending = self._pending_suggestion
        if pending is None:
            return None
        if time.time() - pending.created_at > _SUGGESTION_TTL_SEC:
            self._pending_suggestion = None
            return None
        return pending

    async def _confirm_action(self, prompt: str) -> bool:
        """
        Voice/text-answerable confirmation for gated tool actions (audit C3).

        Speaks the prompt, then waits for the next user utterance to resolve as
        yes/no. Both voice and text modes deliver that answer as a UserInput,
        which _handle_user_input routes into the pending-confirm future. Denies
        on timeout so a gated action can never proceed without an explicit yes.
        """
        if self._pending_confirm is not None and not self._pending_confirm.done():
            return False  # a confirmation is already in flight

        loop = asyncio.get_running_loop()
        self._pending_confirm = loop.create_future()

        self._speaking = True
        try:
            await self._tts.speak(prompt)
        finally:
            self._speaking = False

        try:
            return await asyncio.wait_for(self._pending_confirm, timeout=20.0)
        except asyncio.TimeoutError:
            log.info("Confirmation timed out — denying action")
            self._speaking = True
            try:
                await self._tts.speak("No confirmation heard, Sir. I'll hold off.")
            finally:
                self._speaking = False
            return False
        finally:
            self._pending_confirm = None

    # ── Main pipeline ─────────────────────────────────────────────────────────

    async def _handle_user_input(self, event: UserInput) -> None:
        text = event.text

        if self._speaking and event.source == "stt":
            # Barge-in: a genuine interrupt phrase cuts off JARVIS mid-sentence.
            # Everything else (ambient noise, speaker echo) is suppressed as before,
            # preserving the self-cancellation fix.
            if not _INTERRUPT_RE.search(text):
                log.debug("Suppressed (speaking): %r", text)
                return
            log.info("Barge-in: %r", text)
            await self._interrupt_speech()
            if _STOP_ONLY_RE.match(text):
                self._speaking = True
                try:
                    await self._tts.speak("Stopped, Sir.")
                finally:
                    self._speaking = False
                return
            # Carries a new instruction (e.g. "Jarvis, what's the weather") —
            # fall through and process it as a fresh command.

        log.info("User: %s", text)

        # Confirmation short-circuit — MUST run before the _active_task cancel
        # below, because the confirmation is being awaited *inside* that task.
        # This utterance is the yes/no answer, not a new command.
        if self._pending_confirm is not None and not self._pending_confirm.done():
            approved = bool(_AFFIRMATIVE_RE.match(text))
            self._pending_confirm.set_result(approved)
            return

        # Suggestion confirmation short-circuit
        pending = self._consume_pending_suggestion()
        if pending is not None and event.source != "suggestion":
            if _AFFIRMATIVE_RE.match(text):
                self._pending_suggestion = None
                self._bus.publish(UserInput(
                    text=pending.action_text, source="suggestion", timestamp=time.time(),
                ))
                return
            if _NEGATIVE_RE.match(text):
                self._pending_suggestion = None
                self._speaking = True
                try:
                    await self._tts.speak("Understood, Sir.")
                finally:
                    self._speaking = False
                return
            self._pending_suggestion = None

        # Goal actions (create / complete) are handled here, synchronously, as
        # the single owner — so a goal notice and an LLM reply can never both
        # fire for the same utterance (the old parallel-subscription bug).
        if event.source != "suggestion":
            goal_reply = await self._goal_manager.handle_input(text)
            if goal_reply is not None:
                self._agent_state.update_from_input(text)  # keep the idle clock fresh
                log.info("Goal action: %s", goal_reply)
                self._speaking = True
                try:
                    await self._tts.speak(goal_reply)
                finally:
                    self._speaking = False
                return

        # Interrupt any in-flight execution
        if self._active_task and not self._active_task.done():
            self._active_task.cancel()
            await self._action.stop_tts()
            log.info("Cancelled previous execution")

        # 1. Update internal state
        self._agent_state.update_from_input(text)
        state = self._agent_state.snapshot()
        self._bus.publish(StateUpdated(snapshot=state))

        # 2. Build LLM messages — vault memory + (for factual queries) web knowledge
        messages = await self._vault.build_messages(text, intent=state.conversation_intent)

        # 3. Decision
        decision = await self._decision_engine.decide(
            text     = text,
            state    = state,
            memories = [],
            messages = messages,
        )
        log.info(
            "Decision: type=%s priority=%d reason=%s",
            decision.action_type, decision.priority, decision.reason,
        )
        self._bus.publish(DecisionReady(
            decision=decision, original_text=text, messages=messages,
        ))

        # 4. Micro-acknowledgement
        if decision.action_type not in ("ignore", "tool", "plan") and \
                state.conversation_intent not in ("gratitude", "chitchat"):
            self._speaking = True
            await self._tts.enqueue(random.choice(_ACK_PHRASES))

        # 5. Execute
        self._active_task = asyncio.create_task(
            self._execute_decision(decision, text, messages)
        )
        try:
            response = await self._active_task
        except asyncio.CancelledError:
            return

        # 6. Persist (Phase A: VaultAdapter stub no-ops; Phase B: real write)
        if response:
            self._bus.publish(StreamComplete(response=response, original_text=text))
            log.info("Jarvis: %s", response[:120])

    # ── Decision execution ────────────────────────────────────────────────────

    async def _execute_decision(
        self,
        decision: Decision,
        text: str,
        messages: list[dict],
    ) -> str:

        # Plan (compound FastRouter)
        if decision.action_type == "plan":
            responses = []
            for route in decision.content:
                await asyncio.sleep(0)
                if asyncio.current_task().cancelled():
                    break
                log.info("Plan step: %s args=%s", route.tool, route.args)
                self._bus.publish(ToolRequest(tool=route.tool, args=route.args, request_id=route.tool))
                result = await self._action.execute_tool(route.tool, route.args)
                self._bus.publish(ToolResult(
                    tool=route.tool, request_id=route.tool,
                    status=result.get("status", ""), result=result.get("result"),
                ))
                if result.get("status") != "ok":
                    responses.append(_format_fast_result(route.tool, result))
                    break
                responses.append(_format_fast_result(route.tool, result))
            response = " ".join(responses)
            self._speaking = True
            try:
                await self._tts.speak(response)
            finally:
                self._speaking = False
            return response

        # Tool
        if decision.action_type == "tool":
            route = decision.content

            # list_goals — Phase A: no GoalManager → respond with a placeholder.
            if route.tool == "list_goals":
                active = [g for g in self._agent_state.active_goals if g.is_active]
                if not active:
                    response = "You have no active goals at the moment, Sir."
                else:
                    names = ", ".join(g.name for g in active)
                    response = f"Your active goals: {names}."
                self._speaking = True
                try:
                    await self._tts.speak(response)
                finally:
                    self._speaking = False
                return response

            log.info("Tool start: %s args=%s", route.tool, route.args)
            self._bus.publish(ToolRequest(tool=route.tool, args=route.args, request_id=route.tool))
            result = await self._action.execute_tool(route.tool, route.args)
            log.info("Tool done:  %s status=%s", route.tool, result.get("status"))
            self._bus.publish(ToolResult(
                tool=route.tool, request_id=route.tool,
                status=result.get("status", ""), result=result.get("result"),
            ))
            response = _format_fast_result(route.tool, result)
            self._speaking = True
            try:
                await self._tts.speak(response)
            finally:
                self._speaking = False
            return response

        # Direct speak (rule-based)
        if decision.action_type == "speak":
            content = str(decision.content)
            self._speaking = True
            try:
                await self._tts.speak(content)
            finally:
                self._speaking = False
            return content

        # LLM stream
        if decision.action_type == "llm":
            response = await self._stream_response(messages)
            # Action enforcement: re-route faked tool calls (one shot)
            if decision.reason != "llm_guard" and \
                    re.search(r"\b(opening|launching|starting|playing)\b", response, re.IGNORECASE):
                route = self._fast_router.route(response)
                if route and not isinstance(route, list):
                    log.warning("LLM faked action — re-routing via FastRouter: %s", route.tool)
                    return await self._execute_decision(
                        Decision(action_type="tool", priority=PRIORITY_HIGH,
                                 content=route, should_act=True, reason="llm_guard"),
                        text, messages,
                    )
            return response

        return ""

    # ── Dynamic system prompt ─────────────────────────────────────────────────

    def _current_system_prompt(self) -> str:
        """Build a fresh system prompt with live context: time, goals, mood,
        system load, and what Sir currently has on screen (Phase F)."""
        now = datetime.now()
        now_str = now.strftime("%A, %d %B · %I:%M %p").lstrip("0")
        snap = self._agent_state.snapshot()

        active_goals = [g.name for g in self._agent_state.active_goals if g.is_active]

        return build_system_prompt(
            now_str        = now_str,
            active_goals   = active_goals or None,
            current_focus  = snap.current_focus or "",
            system_state   = snap.system_state,
            screen_context = self._screen_context(),
        )

    @staticmethod
    def _screen_context() -> str:
        """Foreground window title, trimmed. Empty on headless / failure."""
        try:
            from action.tools.screen_tool import active_window_title
            return active_window_title()[:80]
        except Exception:
            return ""

    # ── LLM streaming ─────────────────────────────────────────────────────────

    async def _stream_response(self, messages: list[dict]) -> str:
        sentences: list[str] = []
        tool_triggered = False
        sys_prompt = self._current_system_prompt()

        self._speaking = True
        try:
            last_message_out: list = []
            async for sentence in self._llm.sentence_stream(
                messages,
                tools=TOOL_SCHEMAS,
                system_prompt=sys_prompt,
                last_message_out=last_message_out,
            ):
                cleaned = _clean(sentence)
                if cleaned:
                    sentences.append(cleaned)
                    await self._tts.enqueue(cleaned)
                    print(cleaned, end=" ", flush=True)

            print()

            raw_msg = last_message_out[0] if last_message_out else {}
            if raw_msg.get("tool_calls"):
                tool_triggered = True

            if tool_triggered:
                result = await self._agent_core.run(messages)
                response = result.response
                if result.used_any_tools:
                    log.info(
                        "AgentCore: tools=%s turns=%d stop=%s",
                        result.used_tools, result.turns, result.stop_reason,
                    )
                await self._tts.speak(response)
                return response

            await self._action.drain()
            return " ".join(sentences) or "I'm not sure how to respond to that, Sir."
        finally:
            self._speaking = False

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _internal_loop(self) -> None:
        """
        Ambient presence — periodically consider volunteering something.
        Stays silent unless DecisionEngine.generate_internal_thought decides
        it's warranted (user idle, system normal, low probability), and never
        talks over active speech or an in-flight command.
        """
        log.info("Internal loop started (interval=%ds)", _INTERNAL_LOOP_INTERVAL)
        while self._running:
            try:
                await asyncio.sleep(_INTERNAL_LOOP_INTERVAL)

                if self._speaking or (self._active_task and not self._active_task.done()):
                    continue

                state   = self._agent_state.snapshot()
                thought = await self._decision_engine.generate_internal_thought(
                    state=state, memories=[],
                )
                if thought.should_act and thought.content:
                    log.info("Proactive thought: %s", thought.content[:60])
                    self._speaking = True
                    try:
                        await self._tts.speak(thought.content)
                    finally:
                        self._speaking = False

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Internal loop error: %s", e)

    async def _interrupt_speech(self) -> None:
        """Cut off in-flight speech and execution for a barge-in."""
        self._speaking = False
        if self._active_task and not self._active_task.done():
            self._active_task.cancel()
        try:
            await self._tts.interrupt()
        except Exception as e:
            log.warning("TTS interrupt failed: %s", e)

    async def _tts_speak(self, text: str) -> None:
        """TTS callback for AgentExecutor (timer alerts, etc.)."""
        self._speaking = True
        try:
            await self._tts.speak(text)
        except Exception as e:
            log.warning("TTS callback failed: %s", e)
        finally:
            self._speaking = False
