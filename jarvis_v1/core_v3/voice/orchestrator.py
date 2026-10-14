"""
VoiceOrchestrator — drives IDLE → LISTENING → PROCESSING → SPEAKING → (INTERRUPTED).

Concurrency model (no blocking pipeline):
  - One MicrophoneInput fans frames out to whichever consumer the current state
    needs (wake detector / endpointer / interruption monitor).
  - Each phase is an awaited, cancellable coroutine. Transitions cancel cleanly.
  - SPEAKING runs TWO tasks at once: one produces+plays speech (cognition →
    TTS → player, sentence by sentence), the other watches the mic for barge-in.
    A barge-in stops the player instantly, cancels the cognition task (aborting
    the Ollama stream), and loops back into LISTENING carrying the interrupting
    audio as look-back — so nothing the user said is lost.

The wake word is required again after each turn (returns to IDLE). An
"active conversation mode" could later skip the IDLE→wake step without touching
the rest of the machine.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime

from core_v3.voice.state import VoiceState, VoiceStateManager

log = logging.getLogger("jarvis.voice")

_AFFIRMATIVE_RE = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|sure|ok|okay|please|do it|go ahead|affirmative)\b", re.IGNORECASE
)
# A wake phrase that bled into the capture ("Hey Jarvis, open Chrome").
_WAKE_PREFIX_RE = re.compile(
    r"^\s*(?:(?:hey|hi|ok|okay)[\s,]+)?(?:jarvis|echo)\b[\s,.!?]*", re.IGNORECASE
)
# A bare "stop"-type utterance: end the turn quietly instead of asking the LLM.
_STOP_ONLY_RE = re.compile(
    r"^\s*(?:stop|cancel|never\s?mind|forget it|that'?s (?:all|it)|quiet|shut up|enough)[\s.!,]*$",
    re.IGNORECASE,
)


def _time_of_day(now: datetime) -> str:
    h = now.hour
    return "morning" if h < 12 else "afternoon" if h < 17 else "evening"


class VoiceOrchestrator:
    def __init__(
        self, *, mic, wake, endpointer, stt, tts, player, interruption,
        conversation, voice_config, resource_snapshot=None,
    ) -> None:
        self._mic = mic
        self._wake = wake
        self._endpointer = endpointer
        self._stt = stt
        self._tts = tts
        self._player = player
        self._interruption = interruption
        self._conversation = conversation
        self._cfg = voice_config
        self._resource_snapshot = resource_snapshot

        self.state = VoiceStateManager()
        self._running = False
        self._confirming = False          # suppresses barge-in during confirmation
        self._last_sentence = ""          # last spoken sentence (follow-up detection)

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._running = True
        await self._mic.start()
        await self._player.start()
        await self._greet()
        await self.run()

    async def stop(self) -> None:
        self._running = False
        self._player.interrupt()
        await self._player.shutdown()
        await self._mic.stop()

    # ── Main loop ───────────────────────────────────────────────────────────
    async def run(self) -> None:
        while self._running:
            try:
                q = await self._await_wake()
                if not self._running:
                    break
                # Chime plays WITHOUT blocking capture, and we keep the SAME mic
                # subscription straight into LISTENING — so a command spoken right
                # after the wake word ("hey jarvis, open Chrome") isn't clipped.
                if self._cfg.chime_on_wake:
                    asyncio.create_task(self._chime())
                await self._conversation_turn(initial_q=q)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("voice loop error: %s", e, exc_info=True)
                self.state.transition(VoiceState.IDLE)

    async def _await_wake(self) -> asyncio.Queue | None:
        """Wait for the wake word. Returns the LIVE mic subscription (still
        receiving frames) so the caller can capture the command with no gap."""
        self.state.transition(VoiceState.IDLE)
        if not self._wake.available:
            # No wake model: wait briefly and treat silence as no-op (text mode
            # should be used instead). Avoids a hot loop.
            await asyncio.sleep(0.5)
            return None
        self._wake.reset()
        q = self._mic.subscribe()
        print('\n[Echo] Idle — say "hey jarvis"\n', flush=True)
        # Instrumentation: every ~4s report frames seen + peak score, so if wake
        # never fires we can tell whether frames are arriving (mic/loop healthy)
        # and how close the score got (threshold tuning) vs. a dead stream.
        frames = 0
        peak = 0.0
        last_report = time.perf_counter()
        while self._running:
            frame = await q.get()
            frames += 1
            score = self._wake.score(frame)
            peak = max(peak, score)
            if score >= self._wake.threshold:
                log.info("wake word detected (score=%.2f)", score)
                return q
            now = time.perf_counter()
            if now - last_report >= 4.0:
                log.info("wake idle: %d frames in 4s, peak score=%.2f (thr=%.2f)",
                         frames, peak, self._wake.threshold)
                frames = 0
                peak = 0.0
                last_report = now
        self._mic.unsubscribe(q)
        return None

    async def _conversation_turn(self, initial_q: asyncio.Queue | None = None) -> None:
        """One wake-initiated interaction: listen → process → speak, looping back
        to LISTENING on barge-in, and to IDLE when the turn completes."""
        preroll: list | None = None
        followup = False        # True while waiting for the answer to Echo's question
        while self._running:
            # LISTENING — reuse the live wake subscription for the FIRST capture
            # (no gap → no clipped command onset); fresh subscription afterward.
            self.state.transition(VoiceState.LISTENING)
            if followup:
                # Cue that Echo is waiting for an answer. The cue finishes BEFORE
                # we subscribe, so neither it nor the tail of Echo's reply is
                # recorded as the user's answer.
                if getattr(self._cfg, "follow_up_chime", True):
                    await self._chime()
                else:
                    await asyncio.sleep(0.15)
                print("[Echo] Listening for your reply...", flush=True)
            else:
                print("[Echo] Listening...", flush=True)
            if initial_q is not None:
                q, initial_q = initial_q, None
            else:
                q = self._mic.subscribe()
            timeout = getattr(self._cfg, "follow_up_timeout_sec", 6.0) if followup else None
            try:
                audio, reason = await self._endpointer.capture(
                    q, preroll=preroll, onset_timeout_sec=timeout,
                )
            finally:
                self._mic.unsubscribe(q)
            preroll = None
            if audio is None:
                log.info("no %s captured (%s) → idle", "reply" if followup else "speech", reason)
                return

            # PROCESSING
            self.state.transition(VoiceState.PROCESSING)
            text = await self._stt.transcribe(audio, allow_answers=followup)
            if not text.strip():
                log.info("empty transcript → idle")
                return
            print(f"[Heard] {text}", flush=True)

            command = _WAKE_PREFIX_RE.sub("", text, count=1).strip()
            if not command:
                # Only the wake word was captured — keep listening for the
                # actual command (bounded by the endpointer's onset timeout).
                log.info("only the wake word was heard → still listening")
                continue
            if _STOP_ONLY_RE.match(command):
                log.info("stop phrase (%r) → idle", command)
                return

            # SPEAKING (+ barge-in). Returns interrupting look-back, or None.
            barge = await self._speak_response(command, followup=followup)
            if barge is None:
                if self._should_follow_up():
                    # Echo asked a question — wait for the answer without
                    # requiring the wake word again.
                    followup = True
                    continue
                return                      # turn complete → IDLE
            self.state.transition(VoiceState.INTERRUPTED)
            followup = False
            preroll = barge                 # carry interrupting speech into LISTENING

    def _should_follow_up(self) -> bool:
        """Listen again without the wake word? "question" (default): only when
        Echo's reply ended with a question. "always": after every reply.
        "off": never."""
        mode = getattr(self._cfg, "follow_up", "question")
        if mode == "always":
            return True
        if mode == "question":
            return self._last_sentence.rstrip().endswith("?")
        return False

    async def _speak_response(self, text: str, *, followup: bool = False) -> list | None:
        """Stream the response to TTS while monitoring for barge-in.
        Returns the interrupting frames if the user barged in, else None."""
        self.state.transition(VoiceState.SPEAKING)
        self._last_sentence = ""
        produce = asyncio.create_task(self._produce_speech(text, followup=followup))
        playback = asyncio.create_task(self._await_playback(produce))

        monitor = None
        monitor_q = None
        mode = getattr(self._cfg, "barge_in_mode", "wake")
        if self._cfg.allow_barge_in and mode != "off":
            monitor_q = self._mic.subscribe()
            monitor = asyncio.create_task(self._monitor_barge_in(monitor_q))

        waiters = {playback} | ({monitor} if monitor else set())
        try:
            done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if monitor is not None and monitor in done and monitor.result() is not None:
                # Barge-in: stop everything immediately.
                self._player.interrupt()
                produce.cancel()
                playback.cancel()
                await asyncio.gather(produce, playback, return_exceptions=True)
                return monitor.result()
            # Completed normally.
            return None
        finally:
            for t in (monitor, produce, playback):
                if t is not None and not t.done():
                    t.cancel()
            await asyncio.gather(*[t for t in (monitor, produce, playback) if t], return_exceptions=True)
            if monitor_q is not None:
                self._mic.unsubscribe(monitor_q)

    async def _produce_speech(self, text: str, *, followup: bool = False) -> None:
        """Cognition → TTS → player, sentence by sentence (overlapped)."""
        async for sentence in self._conversation.respond(text, followup=followup):
            self._last_sentence = sentence
            seg = await self._tts.synthesize(sentence)
            if seg is not None:
                self._player.enqueue(*seg)
            print(f"  … {sentence}", flush=True)

    async def _await_playback(self, produce: asyncio.Task) -> None:
        await produce                       # all sentences enqueued (or cancelled)
        await self._player.wait_idle()      # ...and fully played out

    async def _monitor_barge_in(self, q: asyncio.Queue) -> list | None:
        if getattr(self._cfg, "barge_in_mode", "wake") == "wake":
            return await self._monitor_wake_barge_in(q)
        # "vad" mode: any sustained speech interrupts (reliable with headphones;
        # on open speakers Echo's own voice can trigger it).
        while True:
            frames = await self._interruption.monitor(q)
            if self._confirming:
                continue                    # ignore the user's confirmation speech
            log.info("barge-in → interrupting speech")
            return frames

    async def _monitor_wake_barge_in(self, q: asyncio.Queue) -> list:
        """Interrupt only when the wake word is spoken over Echo. Echo never says
        "hey jarvis" itself, so its own voice leaking from open speakers into the
        mic can't self-trigger — which is what made "any speech" barge-in loop.
        Returns an empty look-back: the wake phrase isn't part of the command."""
        if not self._wake.available:
            await asyncio.Event().wait()    # no wake model → never barge in
        self._wake.reset()
        while True:
            frame = await q.get()
            if self._confirming:
                continue
            if self._wake.triggered(frame):
                log.info("barge-in (wake word) → interrupting speech")
                return []

    # ── Callbacks for AgentExecutor (speak / fail-closed confirm) ──────────────
    async def speak(self, text: str) -> None:
        """Blocking speak used by the executor (timer alerts) and the greeting.
        No barge-in monitoring — these are short and system-initiated."""
        seg = await self._tts.synthesize(text)
        if seg is not None:
            self._player.enqueue(*seg)
            await self._player.wait_idle()

    async def confirm(self, prompt: str) -> bool:
        """Voice-answerable confirmation for gated tools. Speaks the prompt, then
        captures ONE utterance and matches yes/no. Fails closed (denies) on
        timeout or empty, so a gated action never proceeds without an explicit
        yes."""
        self._confirming = True
        try:
            await self.speak(prompt)
            q = self._mic.subscribe()
            try:
                audio, _ = await asyncio.wait_for(self._endpointer.capture(q), timeout=15.0)
            except asyncio.TimeoutError:
                await self.speak("No confirmation heard, Sir. I'll hold off.")
                return False
            finally:
                self._mic.unsubscribe(q)
            if audio is None:
                return False
            answer = await self._stt.transcribe(audio)
            return bool(_AFFIRMATIVE_RE.match(answer))
        finally:
            self._confirming = False

    # ── Greeting / chime ──────────────────────────────────────────────────────
    async def _greet(self) -> None:
        now = datetime.now()
        extra = ""
        if self._resource_snapshot is not None:
            try:
                snap = self._resource_snapshot()
                extra = f" CPU at {snap.get('cpu_pct', 0):.0f} percent."
            except Exception:
                pass
        greeting = (f"Good {_time_of_day(now)}, Sir. All systems are online. "
                    f"It is {now.strftime('%I:%M %p').lstrip('0')}.{extra}")
        log.info("Greeting: %s", greeting)
        await self.speak(greeting)

    async def _chime(self) -> None:
        try:
            from perception.cues import play_chime
            await asyncio.to_thread(play_chime)
        except Exception as e:
            log.debug("chime failed: %s", e)
