"""
BBP command handlers — what the Brain does when a Body sends a command.

  input.text     typed input. Same path as text mode; the reply streams to the
                 HUD as reply.* and is spoken only if bbp.speak_text_input.
  voice.control  wake (skip the wake word) · mute / unmute (microphone off/on;
                 typed input still works) · cancel (stop the current turn)
  ui.hotkey      push_to_talk → wake (tap-to-talk)

A handler refuses a command by raising BBPError; BBPServer answers the Body
with an error response (E_BUSY, E_NOT_ALLOWED, E_NOT_FOUND). Accepted commands
get no response — their effect shows up as voice.state / reply.* events.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from core_v3.adapters.bbp_protocol import BBPError

log = logging.getLogger("jarvis.bbp")


class BBPCommands:
    def __init__(
        self, *, orchestrator, conversation, config, voice_active: Callable[[], bool],
        turn_lock: asyncio.Lock,
    ) -> None:
        self._orch = orchestrator
        self._conv = conversation
        self._cfg = config                  # BBPConfig
        self._voice_active = voice_active   # False in text mode (no mic, no voice loop)
        self._turn_lock = turn_lock         # serialises text-mode turns (stdin + Body)

    def register(self, server) -> None:
        server.register_command("input.text", self.input_text)
        server.register_command("voice.control", self.voice_control)
        server.register_command("ui.hotkey", self.ui_hotkey)

    # ── input.text ────────────────────────────────────────────────────────────
    async def input_text(self, client, message: dict[str, Any]) -> None:
        text = message["payload"]["text"].strip()
        log.info("BBP: typed input from %s: %r", client.client, text)
        if self._voice_active():
            # Through the voice loop, so it never overlaps a spoken turn.
            if not self._orch.request_text(text, speak=self._cfg.speak_text_input):
                state = self._orch.state.state.value.lower()
                raise BBPError("E_BUSY", f"JARVIS is {state}; try again in a moment")
            return
        if self._turn_lock.locked():
            raise BBPError("E_BUSY", "JARVIS is answering another message; try again in a moment")
        async with self._turn_lock:
            print(f"\n[HUD] You: {text}", flush=True)
            async for sentence in self._conv.respond(text):
                print(f"JARVIS: {sentence}", flush=True)

    # ── voice.control ─────────────────────────────────────────────────────────
    async def voice_control(self, client, message: dict[str, Any]) -> None:
        action = message["payload"]["action"]
        log.info("BBP: voice.control %s from %s", action, client.client)
        self._require_voice()
        if action == "wake":
            self._wake()
        elif action == "mute":
            await self._orch.set_muted(True)
        elif action == "unmute":
            try:
                await self._orch.set_muted(False)
            except Exception as e:
                raise BBPError("E_NOT_FOUND", f"microphone unavailable: {e}") from None
        elif action == "cancel":
            self._orch.cancel_turn()

    # ── ui.hotkey ─────────────────────────────────────────────────────────────
    async def ui_hotkey(self, client, message: dict[str, Any]) -> None:
        name = message["payload"]["name"]
        log.info("BBP: hotkey %s from %s", name, client.client)
        if name != "push_to_talk":
            raise BBPError("E_NOT_FOUND", f"no action is mapped to hotkey {name!r}")
        self._require_voice()
        self._wake()

    # ── helpers ───────────────────────────────────────────────────────────────
    def _require_voice(self) -> None:
        if not self._voice_active():
            raise BBPError("E_NOT_ALLOWED", "voice is off: JARVIS is running in text mode")

    def _wake(self) -> None:
        if self._orch.muted:
            raise BBPError("E_NOT_ALLOWED", "the microphone is muted")
        if self._orch.request_wake() == "busy":
            raise BBPError("E_BUSY", "JARVIS is thinking; try again in a moment")
