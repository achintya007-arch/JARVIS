"""
core_v2/brain.py — V2 orchestrator (event-driven replacement for assistant.py).

Processing pipeline per utterance (Brain._handle_user_input):

  UserInput event received
    → AgentState.update_from_input()       (sync, O(1))
    → publish SpeakRequest(ack)            (fire-and-forget via bus)
    → MemoryAdapter.recall()               (direct call — needs return value)
    → MemoryAdapter.build_messages()       (direct call — needs return value)
    → DecisionEngine.decide()             (4-stage: FastRouter → Rules → Memory → LLM)
    → _execute_decision():
        "tool"   → ActionAdapter.execute_tool() → format → publish SpeakRequest
        "speak"  → publish SpeakRequest
        "llm"    → LLMClient.sentence_stream() → per-sentence SpeakRequest
                     → if tool_calls: AgentCore.run() → SpeakRequest
        "ignore" → noop
    → publish StreamComplete               (triggers MemoryAdapter persistence)

Background:
  _internal_loop() every 30s → DecisionEngine.generate_internal_thought()
                              → publish SpeakRequest(priority=IMPORTANT) if actionable

All existing modules are called with their original APIs — nothing is modified.
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
from cognition.agent_core import AgentCore
from cognition.agent_core.planner import TOOL_SCHEMAS
from cognition.agent_state import AgentState
from cognition.context_manager import ContextManager
from cognition.decision_engine import (
    PRIORITY_HIGH,
    Decision,
    DecisionEngine,
)
from cognition.goal_manager import GoalManager
from cognition.llm_client import LLMClient, build_system_prompt
from core.config import Config
from core_v2.adapters import (
    ActionAdapter,
    MemoryAdapter,
    PerceptionAdapter,
    ResourceAdapter,
    SpeakingGuard,
)
from core_v2.event_bus import EventBus
from core_v2.events import (
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
from perception.hotword import HotwordDetector
from perception.stt import STTEngine
from perception.tts import TTSEngine

log = logging.getLogger("jarvis.brain")

_ACK_PHRASES = [
    "Yes, Sir.",
    "Right away.",
    "One moment, Sir.",
    "Understood.",
    "On it.",
]

_INTERNAL_LOOP_INTERVAL = 4  # seconds

# ── Suggestion confirmation ───────────────────────────────────────────────────
_SUGGESTION_TTL_SEC = 30

_AFFIRMATIVE_RE = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|sure|ok|okay|please|do it|go ahead|affirmative)\b",
    re.IGNORECASE,
)
_NEGATIVE_RE = re.compile(
    r"^\s*(?:no|nope|nah|negative|cancel|skip|nevermind|never mind|don'?t)\b",
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


class Brain:
    """
    V2 orchestrator. Drop-in replacement for Assistant.
    Same interface: Brain(config) → await brain.start().
    """

    def __init__(self, config: Config) -> None:
        self.config   = config
        self._bus     = EventBus()
        self._running = False
        self._stopped = False
        self._internal_loop_task: asyncio.Task | None = None
        self._last_proactive_time = 0

        # ── Infrastructure ────────────────────────────────────────────────
        self._resource_monitor = ResourceMonitor(config.resources)
        self._stt     = STTEngine(config.stt)
        self._tts     = TTSEngine(config.tts)
        self._hotword = HotwordDetector(config.hotword)

        # ── Cognition ─────────────────────────────────────────────────────
        self._llm = LLMClient(config.llm)
        self._context = ContextManager(
            db_path    = config.conversation_db,
            vec_path   = config.vector_store_dir,
            ollama_url = config.llm.base_url,
            embed_model= "nomic-embed-text",
        )
        self._agent_state    = AgentState()
        self._fast_router    = FastRouter()
        self._decision_engine = DecisionEngine(fast_router=self._fast_router)

        # ── Action ────────────────────────────────────────────────────────
        self._executor = AgentExecutor(
            config           = config.agent,
            speak_callback   = self._tts_speak,
            confirm_callback = self._confirm_action,
        )
        self._agent_core = AgentCore(
            llm             = self._llm,
            executor        = self._executor,
            max_turns       = getattr(config, "agent_core_max_turns", 3),
            stream_to_stdout= True,
        )

        # Shared with ActionAdapter (see SpeakingGuard docstring) so bus-mediated
        # speech — resource alerts, goal reminders — also suppresses STT, not
        # just Brain-initiated speech.
        self._speaking_guard = SpeakingGuard()

        # ── Adapters ──────────────────────────────────────────────────────
        self._perception = PerceptionAdapter(self._stt, self._hotword, self._bus, config)
        self._action     = ActionAdapter(self._tts, self._executor, self._bus, self._speaking_guard)
        self._memory     = MemoryAdapter(self._context, self._bus)
        self._resource   = ResourceAdapter(
            self._resource_monitor, self._agent_state, self._llm, config, self._bus
        )

        # Brain listens for user input
        self._bus.subscribe(UserInput, self._handle_user_input)
        self._bus.subscribe(ActionSuggestion, self._handle_action_suggestion)

        # Pending suggestion slot — single slot, ephemeral, auto-expires
        self._pending_suggestion: ActionSuggestion | None = None

        # Confirmation slot — future resolved by the next user utterance (voice
        # or text) when the executor asks to confirm a gated action (audit C3).
        self._pending_confirm: asyncio.Future | None = None

        self._stop_event = asyncio.Event()
        self._active_task: asyncio.Task | None = None

        # ── Goal management ───────────────────────────────────────────────────
        self._goal_manager = GoalManager(
            db_path     = config.conversation_db,
            agent_state = self._agent_state,
            bus         = self._bus,
        )
        self._goal_manager_task: asyncio.Task | None = None

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
        log.info("Brain V2 initialising...")

        await asyncio.gather(
            self._resource_monitor.start(),
            self._stt.initialize(),
            self._tts.initialize(),
            self._context.initialize(),
        )

        await self._goal_manager.initialize()
        self._goal_manager_task = asyncio.create_task(self._goal_manager.run())

        await self._greet()

        self._internal_loop_task = asyncio.create_task(self._internal_loop())

        # publish_and_wait so PerceptionAdapter has started its input loop
        # before start() returns — same sequencing guarantee as V1.
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
        log.info("Brain V2 shutting down...")

        if self._goal_manager_task:
            self._goal_manager_task.cancel()
        if self._internal_loop_task:
            self._internal_loop_task.cancel()
        self._bus.publish(SystemEvent(kind="shutdown"))

        for label, coro in (
            ("goal_manager",     self._goal_manager.close()),
            ("resource_monitor", self._resource_monitor.stop()),
            ("executor",         self._executor.shutdown()),
            ("context",          self._context.close()),
            ("tts",              self._tts.shutdown()),
        ):
            try:
                await coro
            except Exception as e:
                log.warning("Shutdown step '%s' failed: %s", label, e)

        self._stop_event.set()
        log.info("Brain V2 shutdown complete.")


    # ── Startup greeting ──────────────────────────────────────────────────────

    async def _greet(self) -> None:
        now  = datetime.now()
        snap = self._resource.snapshot()
        cpu  = snap.get("cpu_pct", 0)
        ram  = snap.get("ram_pct", 0)
        vram = f" VRAM at {snap['vram_pct']:.0f} percent." if "vram_pct" in snap else ""
        mem  = " Long-term memory is active." if self._memory.memory_ready else ""

        greeting = (
            f"Good {_time_of_day(now)}, Sir. "
            f"It is {now.strftime('%I:%M %p').lstrip('0')} on {now.strftime('%A, %B %d')}. "
            f"All systems are online. "
            f"CPU at {cpu:.0f} percent, RAM at {ram:.0f} percent.{vram}{mem}"
        )
        log.info("Greeting: %s", greeting)
        await self._tts.speak(greeting)

    # ── Main pipeline ─────────────────────────────────────────────────────────

    async def _handle_action_suggestion(self, event: ActionSuggestion) -> None:
        """GoalManager proposes an action — store pending slot + speak the prompt."""
        self._pending_suggestion = event
        log.info("Suggestion pending: %r", event.action_text)
        self._speaking = True
        try:
            await self._tts.speak(event.speak_text)
        finally:
            self._speaking = False

    def _consume_pending_suggestion(self) -> ActionSuggestion | None:
        """Return pending suggestion if present and fresh, else clear and return None."""
        pending = self._pending_suggestion
        if pending is None:
            return None
        if time.time() - pending.created_at > _SUGGESTION_TTL_SEC:
            self._pending_suggestion = None
            log.info("Pending suggestion expired")
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

    async def _handle_user_input(self, event: UserInput) -> None:
        text = event.text

        # While TTS is playing, ignore STT — prevents speaker echo and ambient
        # noise from cancelling the in-flight response before it can be heard.
        if self._speaking and event.source == "stt":
            log.debug("Suppressed (speaking): %r", text)
            return

        log.info("User: %s", text)

        # Confirmation short-circuit — MUST run before the _active_task cancel
        # below, because the confirmation is being awaited *inside* that task.
        # This utterance is the yes/no answer, not a new command.
        if self._pending_confirm is not None and not self._pending_confirm.done():
            approved = bool(_AFFIRMATIVE_RE.match(text))
            self._pending_confirm.set_result(approved)
            return

        # Suggestion confirmation short-circuit — runs before any state update
        # so that "yes"/"no" itself is never LLM-routed or recorded as an exchange.
        pending = self._consume_pending_suggestion()
        if pending is not None and event.source != "suggestion":
            if _AFFIRMATIVE_RE.match(text):
                self._pending_suggestion = None
                log.info("Suggestion accepted → %r", pending.action_text)
                self._bus.publish(UserInput(
                    text      = pending.action_text,
                    source    = "suggestion",
                    timestamp = time.time(),
                ))
                return
            if _NEGATIVE_RE.match(text):
                self._pending_suggestion = None
                log.info("Suggestion declined")
                self._speaking = True
                try:
                    await self._tts.speak("Understood, Sir.")
                finally:
                    self._speaking = False
                return
            # Neither yes nor no — drop the pending slot so a later "yes" in
            # an unrelated context doesn't accidentally fire it.
            self._pending_suggestion = None

        # Goal actions handled here, synchronously, as the single owner — so a
        # goal notice and an LLM reply can never both fire for one utterance.
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

        # 1. Update internal state (sync, O(1))
        self._agent_state.update_from_input(text)
        state = self._agent_state.snapshot()
        self._bus.publish(StateUpdated(snapshot=state))

        # 2. Build LLM messages — recall happens inside build_messages() once.
        # We don't call recall() separately here; _memory_aware stage in
        # DecisionEngine doesn't act on memories, so a second embed round-trip
        # was pure waste.
        memories: list[str] = []
        messages = await self._memory.build_messages(text)

        # 3. Decision (4-stage pipeline)
        decision = await self._decision_engine.decide(
            text     = text,
            state    = state,
            memories = memories,
            messages = messages,
        )
        log.info(
            "Decision: type=%s priority=%d reason=%s",
            decision.action_type, decision.priority, decision.reason,
        )
        self._bus.publish(DecisionReady(
            decision      = decision,
            original_text = text,
            messages      = messages,
        ))

        # 4. Micro-acknowledgement — skip for tool/plan (response is instant) and ignore/gratitude/chitchat
        if decision.action_type not in ("ignore", "tool", "plan") and \
                state.conversation_intent not in ("gratitude", "chitchat"):
            self._speaking = True   # lock before enqueue so echo can't re-trigger
            await self._tts.enqueue(random.choice(_ACK_PHRASES))

        # 5. Execute
        self._active_task = asyncio.create_task(
            self._execute_decision(decision, text, messages)
        )
        try:
            response = await self._active_task
        except asyncio.CancelledError:
            return

        # 6. Persist via MemoryAdapter (event triggers add_exchange)
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

        # Plan (compound FastRouter hit — sequential tools)
        if decision.action_type == "plan":
            responses = []
            for route in decision.content:
                await asyncio.sleep(0)                         # yield — allows CancelledError to propagate
                if asyncio.current_task().cancelled():
                    log.info("Plan interrupted")
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
                    log.warning("Plan aborted at step %s — status=%s", route.tool, result.get("status"))
                    break
                responses.append(_format_fast_result(route.tool, result))
            response = " ".join(responses)
            self._speaking = True
            try:
                await self._tts.speak(response)
            finally:
                self._speaking = False
            return response

        # Tool (FastRouter hit)
        if decision.action_type == "tool":
            route = decision.content                         # RouteResult

            # list_goals — handled here because Brain owns the goal state reference.
            # No need to thread it through AgentExecutor.
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
            result   = await self._action.execute_tool(route.tool, route.args)
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

        # Speak directly (rule-based, no LLM)
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
            if re.search(r"\b(?:open|launch)\b", text, re.IGNORECASE):
                log.warning("LLM handled action — likely missing FastRouter rule: %r", text)
            response = await self._stream_response(messages)
            # Action enforcement: if LLM faked a tool action, re-route it (once only)
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

        # Ignore
        return ""

    # ── Dynamic system prompt ─────────────────────────────────────────────────

    def _current_system_prompt(self) -> str:
        """Build a fresh system prompt with live context for this call."""
        now = datetime.now()
        now_str = now.strftime("%A, %d %B · %I:%M %p").lstrip("0")

        snap = self._agent_state.snapshot()

        active_goals = [
            g.name for g in self._agent_state.active_goals if g.is_active
        ]

        return build_system_prompt(
            now_str       = now_str,
            active_goals  = active_goals or None,
            current_focus = snap.current_focus or "",
            system_state  = snap.system_state,
        )

    # ── LLM streaming ─────────────────────────────────────────────────────────

    async def _stream_response(self, messages: list[dict]) -> str:
        sentences:     list[str] = []
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
                    # Enqueue directly — bypasses event bus to avoid the race where
                    # drain() fires before the handler task has had a chance to run.
                    await self._tts.enqueue(cleaned)
                    print(cleaned, end=" ", flush=True)

            print()

            raw_msg = last_message_out[0] if last_message_out else {}
            if raw_msg.get("tool_calls"):
                tool_triggered = True
                log.info("Tool call detected mid-stream — routing to AgentCore")

            if tool_triggered:
                result   = await self._agent_core.run(messages)
                response = result.response
                if result.used_any_tools:
                    log.info(
                        "AgentCore: tools=%s turns=%d stop=%s %s",
                        result.used_tools, result.turns, result.stop_reason, result.usage,
                    )
                await self._tts.speak(response)
                return response

            await self._action.drain()
            return " ".join(sentences) or "I'm not sure how to respond to that, Sir."
        finally:
            self._speaking = False

    # ── Background proactive loop ─────────────────────────────────────────────

    async def _internal_loop(self) -> None:
        log.info("Internal loop started (interval=%ds)", _INTERNAL_LOOP_INTERVAL)
        while self._running:
            try:
                await asyncio.sleep(_INTERNAL_LOOP_INTERVAL)

                state  = self._agent_state.snapshot()
                # generate_internal_thought doesn't use memories — skip the
                # embed round-trip that was previously happening every 4 seconds.
                thought = await self._decision_engine.generate_internal_thought(
                    state    = state,
                    memories = [],
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

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _tts_speak(self, text: str) -> None:
        """TTS callback for AgentExecutor (e.g. timer alerts)."""
        self._speaking = True
        try:
            await self._tts.speak(text)
        except Exception as e:
            log.warning("TTS callback failed: %s", e)
        finally:
            self._speaking = False


# ── Fast-route result formatter (mirrors assistant.py) ────────────────────────

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

