"""
ConversationEngine — text in, spoken-response sentences out.

This is the clean boundary between the voice layer and cognition. It owns NO
audio: the orchestrator feeds it the transcribed utterance and consumes an async
stream of response sentences (so TTS can start on sentence 1 while the LLM is
still generating). Cancelling the respond() task aborts the in-flight Ollama
stream — which is how barge-in cancels generation.

Everything it calls is the existing, unchanged cognition: GoalManager,
DecisionEngine (+ FastRouter reflex), VaultMemory, AgentExecutor, AgentCore,
LLMClient. No cognition logic is rewritten here — only the orchestration that
used to be tangled into Brain's TTS/mic code.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime

from cognition.agent_core.planner import build_tool_schemas
from cognition.decision_engine import Decision
from cognition.llm_client import build_system_prompt

log = logging.getLogger("jarvis.conversation")

_STRIP_RE = re.compile(r"```.*?```|`[^`]*`|[*_#~>|\\]|\[([^\]]+)\]\([^)]+\)", re.DOTALL)
_FAKED_ACTION_RE = re.compile(r"\b(opening|launching|starting|playing)\b", re.IGNORECASE)


def _clean(text: str) -> str:
    text = _STRIP_RE.sub(r"\1", text)
    text = re.sub(r"\n{2,}", " ", text)
    return re.sub(r" {2,}", " ", text).strip()


def _format_fast_result(tool: str, result: dict) -> str:
    status = result.get("status")
    if status == "denied":
        return f"That action is restricted, Sir. {result.get('reason', '')}".strip()
    if status == "error":
        return f"I encountered an error with that, Sir. {result.get('error', '')}".strip()
    r = result.get("result", "")
    if tool == "get_time":
        return f"It is {r}, Sir."
    return str(r)


class ConversationEngine:
    def __init__(
        self, *, config, llm, agent_state, decision_engine, goal_manager,
        vault_memory, executor, agent_core, fast_router,
    ) -> None:
        self._config = config
        self._llm = llm
        self._agent_state = agent_state
        self._decision = decision_engine
        self._goals = goal_manager
        self._vault = vault_memory
        self._executor = executor
        self._agent_core = agent_core
        self._fast_router = fast_router
        # Offer the FULL tool set to the LLM (core + screen/keyboard) so it can
        # actually type, focus windows, launch apps — the screen tools were
        # implemented but never sent, so the model narrated actions instead of
        # calling them.
        screen_on = getattr(getattr(config, "agent", None), "screen_enabled", True)
        self._tools = build_tool_schemas(screen_enabled=screen_on)

    async def respond(self, text: str) -> AsyncIterator[str]:
        """Yield spoken-response sentences for the utterance `text`. Cancellable."""
        full: list[str] = []
        try:
            # 1. Goals (single synchronous owner — create/complete short-circuit)
            goal_reply = await self._goals.handle_input(text)
            if goal_reply is not None:
                self._agent_state.update_from_input(text)
                full.append(goal_reply)
                yield goal_reply
                return

            # 2. State
            self._agent_state.update_from_input(text)
            state = self._agent_state.snapshot()

            # 3. Decide (reflex/rule need no messages; build only for the LLM path)
            decision = await self._decision.decide(text=text, state=state, memories=[], messages=[])
            messages: list[dict] = []
            if decision.action_type == "llm":
                messages = await self._vault.build_messages(text, intent=state.conversation_intent)
            log.info("Decision: type=%s reason=%s", decision.action_type, decision.reason)

            # 4. Execute → stream sentences
            async for sentence in self._execute(decision, text, messages):
                full.append(sentence)
                yield sentence
        finally:
            # 5. Persist the exchange (best-effort; runs even if cancelled mid-stream)
            response = " ".join(full).strip()
            if response:
                try:
                    await self._vault.add_exchange(text, response)
                except Exception as e:
                    log.error("vault persist failed: %s", e)

    # ── Execution paths ─────────────────────────────────────────────────────
    async def _execute(self, decision: Decision, text: str, messages: list[dict]) -> AsyncIterator[str]:
        if decision.action_type == "plan":
            for route in decision.content:
                result = await self._run_tool(route.tool, route.args)
                yield _format_fast_result(route.tool, result)
                if result.get("status") != "ok":
                    break
            return

        if decision.action_type == "tool":
            route = decision.content
            if route.tool == "list_goals":
                active = [g for g in self._agent_state.active_goals if g.is_active]
                yield ("Your active goals: " + ", ".join(g.name for g in active) + "."
                       if active else "You have no active goals at the moment, Sir.")
                return
            result = await self._run_tool(route.tool, route.args)
            yield _format_fast_result(route.tool, result)
            return

        if decision.action_type == "speak":
            yield str(decision.content)
            return

        if decision.action_type == "llm":
            async for s in self._stream_llm(decision, text, messages):
                yield s
            return

    async def _run_tool(self, tool: str, args: dict) -> dict:
        results = await self._executor.execute([{"tool": tool, "args": args}])
        return results[0] if results else {"tool": tool, "status": "error", "result": "No result"}

    async def _stream_llm(self, decision: Decision, text: str, messages: list[dict]) -> AsyncIterator[str]:
        sys_prompt = self._system_prompt()
        sentences: list[str] = []
        last_message_out: list = []
        async for sentence in self._llm.sentence_stream(
            messages, tools=self._tools, system_prompt=sys_prompt,
            last_message_out=last_message_out,
        ):
            cleaned = _clean(sentence)
            if cleaned:
                sentences.append(cleaned)
                yield cleaned

        raw = last_message_out[0] if last_message_out else {}
        if raw.get("tool_calls"):
            result = await self._agent_core.run(messages)
            if result.response:
                yield _clean(result.response)
            return

        # Action enforcement: the model narrated an action without calling a tool.
        full = " ".join(sentences)
        if decision.reason != "llm_guard" and _FAKED_ACTION_RE.search(full):
            route = self._fast_router.route(full)
            if route and not isinstance(route, list):
                log.warning("LLM narrated an action — routing via FastRouter: %s", route.tool)
                result = await self._run_tool(route.tool, route.args)
                yield _format_fast_result(route.tool, result)

    # ── System prompt ─────────────────────────────────────────────────────────
    def _system_prompt(self) -> str:
        now = datetime.now()
        snap = self._agent_state.snapshot()
        active_goals = [g.name for g in self._agent_state.active_goals if g.is_active]
        return build_system_prompt(
            now_str        = now.strftime("%A, %d %B · %I:%M %p").lstrip("0"),
            active_goals   = active_goals or None,
            current_focus  = snap.current_focus or "",
            system_state   = snap.system_state,
            screen_context = self._screen_context(),
        )

    @staticmethod
    def _screen_context() -> str:
        try:
            from action.tools.screen_tool import active_window_title
            return active_window_title()[:80]
        except Exception:
            return ""
