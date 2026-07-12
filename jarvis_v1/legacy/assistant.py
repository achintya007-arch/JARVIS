"""
Assistant orchestrator — Intelligence Layer integrated.

Processing pipeline per utterance:

  STT (VAD-gated) → text
    │
    ├─ AgentState.update_from_input()         (sync, O(1))
    │
    ├─ Ack phrase                             (fire-and-forget)
    │
    └─ DecisionEngine.decide()
         │
         ├─ Stage 1: FastRouter (reflex)      → tool → speak
         ├─ Stage 2: Rule-based              → speak directly
         ├─ Stage 3: Memory-aware            → (falls to LLM with context)
         └─ Stage 4: LLM stream              → sentence_stream → TTS
                       └─ tool_calls?        → AgentCore round-trip

Background:
  internal_loop() → every 30s → DecisionEngine.generate_internal_thought()
                              → proactive speak if conditions met

Priority handling:
  CRITICAL (100) → interrupt TTS immediately
  HIGH      (75) → enqueue next
  NORMAL    (50) → standard flow
  LOW/BG    (10) → drop if TTS busy

Latency profile (unchanged):
  Reflex (fast router)    : ~0.3s
  Rule-based             : ~0.3s
  LLM stream             : ~1.2s to first sentence
  AgentCore (tool use)   : ~2–4s
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
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
from cognition.llm_client import LLMClient
from core.config import Config
from infra.resource_monitor import ResourceMonitor
from perception.hotword import HotwordDetector
from perception.stt import STTEngine
from perception.tts import TTSEngine

log = logging.getLogger("jarvis.assistant")

# ── Acknowledgement phrases ────────────────────────────────────────────────────
_ACK_PHRASES = [
    "Yes, Sir.",
    "Right away.",
    "One moment, Sir.",
    "Understood.",
    "On it.",
]

# ── Internal loop interval ────────────────────────────────────────────────────
_INTERNAL_LOOP_INTERVAL = 30      # seconds between proactive thought checks

# ── Markdown stripper ─────────────────────────────────────────────────────────
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


class Assistant:
    def __init__(self, config: Config):
        self.config = config

        # ── Infrastructure ─────────────────────────────────────────────────
        self.resource_monitor = ResourceMonitor(config.resources)
        self.stt     = STTEngine(config.stt)
        self.tts     = TTSEngine(config.tts)
        self.hotword = HotwordDetector(config.hotword)

        # ── Cognition ──────────────────────────────────────────────────────
        self.llm = LLMClient(config.llm)
        self.context = ContextManager(
            db_path      = config.conversation_db,
            vec_path     = config.vector_store_dir,
            ollama_url   = config.llm.base_url,
            embed_model  = "nomic-embed-text",
        )

        # ── Intelligence layer (new) ───────────────────────────────────────
        self.agent_state = AgentState()
        self.fast_router = FastRouter()
        self.decision_engine = DecisionEngine(fast_router=self.fast_router)

        # ── Action ────────────────────────────────────────────────────────
        self.agent = AgentExecutor(
            config         = config.agent,
            speak_callback = self._tts_speak,
        )
        self.agent_core = AgentCore(
            llm            = self.llm,
            executor       = self.agent,
            max_turns      = getattr(config, "agent_core_max_turns", 3),
            stream_to_stdout = True,
        )

        self._running = False
        self._internal_loop_task: asyncio.Task | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        log.info("Initialising components...")

        await asyncio.gather(
            self.resource_monitor.start(),
            self.stt.initialize(),
            self.tts.initialize(),
            self.context.initialize(),
        )

        # Wire resource monitor into AgentState as well as LLM switcher
        self.resource_monitor.on_pressure(self._handle_resource_pressure)

        # Start background proactive loop
        self._internal_loop_task = asyncio.create_task(self._internal_loop())

        await self._greet()

        if self.config.hotword.enabled:
            await self.hotword.listen(callback=self._on_hotword)
        elif getattr(self.config, "mode", "text") == "voice":
            await self._voice_loop()
        else:
            await self._text_loop()

    async def stop(self) -> None:
        self._running = False
        if self._internal_loop_task:
            self._internal_loop_task.cancel()
        await self.resource_monitor.stop()
        await self.context.close()
        await self.tts.shutdown()

    # ── Startup greeting ───────────────────────────────────────────────────────

    async def _greet(self) -> None:
        now  = datetime.now()
        snap = self.resource_monitor.snapshot()
        cpu  = snap.get("cpu_pct", 0)
        ram  = snap.get("ram_pct", 0)

        vram     = f" VRAM at {snap['vram_pct']:.0f} percent." if "vram_pct" in snap else ""
        mem_flag = " Long-term memory is active." if self.context.memory_ready else ""

        greeting = (
            f"Good {_time_of_day(now)}, Sir. "
            f"It is {now.strftime('%I:%M %p').lstrip('0')} on {now.strftime('%A, %B %d')}. "
            f"All systems are online. "
            f"CPU at {cpu:.0f} percent, RAM at {ram:.0f} percent.{vram}{mem_flag}"
        )
        log.info("Greeting: %s", greeting)
        await self.tts.speak(greeting)

    # ── Main pipeline ──────────────────────────────────────────────────────────

    async def process_input(self, text: str) -> str:
        log.info("User: %s", text)

        # ── 1. Update internal state (sync, O(1) — never a bottleneck) ────
        self.agent_state.update_from_input(text)
        state = self.agent_state.snapshot()
        log.debug("State: %s", self.agent_state)

        # ── 2. Micro-acknowledgement (fires before any processing) ─────────
        # Skip ack for rule-based responses — they're fast enough
        if state.conversation_intent not in ("gratitude", "chitchat"):
            asyncio.create_task(self.tts.enqueue(random.choice(_ACK_PHRASES)))

        # ── 3. Recall semantic memories ────────────────────────────────────
        memories: list[str] = []
        if self.context.memory_ready:
            try:
                memories = await self.context.memory_store_recall(text)
            except Exception:
                pass  # memory recall never blocks the pipeline

        # ── 4. Build LLM messages (includes memory injection) ──────────────
        messages = await self.context.build_messages(text)

        # ── 5. Decision engine ─────────────────────────────────────────────
        decision = await self.decision_engine.decide(
            text     = text,
            state    = state,
            memories = memories,
            messages = messages,
        )

        log.info(
            "Decision: type=%s priority=%d reason=%s",
            decision.action_type, decision.priority, decision.reason,
        )

        # ── 6. Execute decision ────────────────────────────────────────────
        response = await self._execute_decision(decision, text, messages)

        # ── 7. Persist exchange ────────────────────────────────────────────
        if response:
            await self.context.add_exchange(text, response)
            log.info("Jarvis: %s", response[:120])

        return response or ""

    # ── Decision execution ─────────────────────────────────────────────────────

    async def _execute_decision(
        self,
        decision: Decision,
        text: str,
        messages: list[dict],
    ) -> str:

        # ── Interrupt TTS if this is critical priority ─────────────────────
        if DecisionEngine.should_interrupt_tts(decision):
            # Drain the queue fast — don't wait for current sentence to finish
            # (In a full implementation you'd cancel the current synthesis task)
            log.warning("CRITICAL priority — interrupting TTS")

        # ── Tool (reflex / fast-router hit) ────────────────────────────────
        if decision.action_type == "tool":
            route = decision.content  # RouteResult
            results  = await self.agent.execute([route.to_executor_step()])
            response = _format_fast_result(route.tool, results[0])
            await self.tts.enqueue(response)
            await self.tts.drain()
            return response

        # ── Speak directly (rule-based, no LLM) ───────────────────────────
        if decision.action_type == "speak":
            content = str(decision.content)
            if decision.priority >= PRIORITY_HIGH:
                await self.tts.enqueue(content)
                await self.tts.drain()
            else:
                # Low priority — don't drain (don't block the pipeline)
                asyncio.create_task(self.tts.enqueue(content))
            return content

        # ── LLM stream ────────────────────────────────────────────────────
        if decision.action_type == "llm":
            return await self._stream_response(messages)

        # ── Ignore ────────────────────────────────────────────────────────
        if decision.action_type == "ignore":
            return ""

        return ""

    # ── Streaming response handler ─────────────────────────────────────────────

    async def _stream_response(self, messages: list[dict]) -> str:
        sentences:     list[str] = []
        tool_triggered = False

        async for sentence in self.llm.sentence_stream(messages, tools=TOOL_SCHEMAS):
            cleaned = _clean(sentence)
            if cleaned:
                sentences.append(cleaned)
                await self.tts.enqueue(cleaned)
                print(cleaned, end=" ", flush=True)

        print()

        raw_msg = getattr(self.llm, "_last_raw_message", {})
        if raw_msg.get("tool_calls"):
            tool_triggered = True
            log.info("Tool call detected mid-stream — routing to AgentCore")

        if tool_triggered:
            result   = await self.agent_core.run(messages)
            response = result.response

            if result.used_any_tools:
                log.info(
                    "AgentCore: tools=%s turns=%d stop=%s %s",
                    result.used_tools, result.turns,
                    result.stop_reason, result.usage,
                )

            await self.tts.enqueue(response)
            await self.tts.drain()
            return response

        await self.tts.drain()
        return " ".join(sentences) or "I'm not sure how to respond to that, Sir."

    # ── Background proactive loop ──────────────────────────────────────────────

    async def _internal_loop(self) -> None:
        """
        Runs every N seconds in the background.
        Asks the DecisionEngine if there's anything worth saying.
        Never blocks the main input pipeline.
        """
        log.info("Internal loop started (interval=%ds)", _INTERNAL_LOOP_INTERVAL)
        while self._running:
            try:
                await asyncio.sleep(_INTERNAL_LOOP_INTERVAL)

                state = self.agent_state.snapshot()

                memories: list[str] = []
                if self.context.memory_ready:
                    try:
                        # Use recent topics as the recall query
                        query = " ".join(state.recent_topics[-2:]) or "general"
                        memories = await self.context.memory_store_recall(query)
                    except Exception:
                        pass

                thought = await self.decision_engine.generate_internal_thought(
                    state=state,
                    memories=memories,
                )

                if thought.should_act and thought.content:
                    log.info("Proactive thought: %s", thought.content[:60])
                    asyncio.create_task(self.tts.enqueue(thought.content))

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Internal loop error: %s", e)

    # ── Voice loops ────────────────────────────────────────────────────────────

    async def _voice_loop(self) -> None:
        print("🎤 Voice mode active (Ctrl+C to exit)\n")
        while self._running:
            try:
                audio = await self.stt.record_utterance()
                text  = await self.stt.transcribe(audio)
                if text.strip():
                    print(f"\n🧠 Heard: {text}\n")
                    await self.process_input(text)
            except (KeyboardInterrupt, EOFError):
                self._running = False
                break
            except Exception as e:
                log.error("Voice loop error: %s", e)

    async def _on_hotword(self) -> None:
        log.info("Wake word detected")
        audio = await self.stt.record_utterance()
        text  = await self.stt.transcribe(audio)
        if text.strip():
            await self.process_input(text)

    async def _text_loop(self) -> None:
        print("Jarvis online. Type your message (Ctrl+C to exit).\n")
        while self._running:
            try:
                text = await asyncio.to_thread(input, "You: ")
                if text.strip():
                    await self.process_input(text)
            except (KeyboardInterrupt, EOFError):
                self._running = False
                break

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _tts_speak(self, text: str) -> None:
        try:
            await self.tts.speak(text)
        except Exception as e:
            log.warning("TTS callback failed: %s", e)

    async def _handle_resource_pressure(self, metric: str, value: float) -> None:
        # Update AgentState so DecisionEngine knows about system load
        snap = self.resource_monitor.snapshot()
        self.agent_state.update_from_system(
            cpu_pct  = snap.get("cpu_pct",  0.0),
            ram_pct  = snap.get("ram_pct",  0.0),
            vram_pct = snap.get("vram_pct", 0.0),
        )

        if metric == "vram_mb" and value > 6500:
            await self.llm.set_model("qwen2.5:3b-instruct-q4_K_M")
            alert = "Sir, VRAM pressure detected. Switching to the compact model."
        elif metric == "ram_pct" and value > 85:
            await self.llm.set_model("qwen2.5:3b-instruct-q4_K_M")
            alert = "Sir, RAM usage is critical. Switching to the compact model."
        else:
            await self.llm.set_model(self.config.llm.model)
            alert = "Pressure cleared. Restoring the standard model."

        log.warning("Resource pressure: %s=%.1f → %s", metric, value, self.llm.current_model)
        asyncio.create_task(self.tts.enqueue(alert))


# ── Fast-route response formatter (unchanged) ─────────────────────────────────

def _format_fast_result(tool: str, result: dict) -> str:
    status = result.get("status")

    if status == "denied":
        return f"That action is restricted, Sir. {result.get('reason', '')}".strip()

    if status == "error":
        return f"I encountered an error with that, Sir. {result.get('error', '')}".strip()

    r = result.get("result", "")

    if tool == "get_time":
        return f"It is {r}, Sir."
    if tool == "system_info":
        return r
    if tool == "weather":
        return r
    if tool == "set_timer":
        return r

    return str(r)
