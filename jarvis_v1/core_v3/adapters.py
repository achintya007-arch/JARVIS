"""
core_v3/adapters.py — ResourceAdapter only.

The former PerceptionAdapter (wake loop / capture) and ActionAdapter (TTS) were
removed in the voice rebuild — that responsibility now lives in core_v3/voice/
(VoiceOrchestrator) and the perception/ primitives. VaultAdapter was removed
too: ConversationEngine persists exchanges to VaultMemory directly.

ResourceAdapter stays: it turns resource-pressure transitions into model
switching (the functional, non-voice part) and a spoken SpeakRequest that the
orchestrator voices when idle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core_v3.event_bus import EventBus
from core_v3.events import PRIORITY_CRITICAL, ResourceAlert, SpeakRequest

if TYPE_CHECKING:
    from cognition.agent_state import AgentState
    from cognition.llm_client import LLMClient
    from core.config import Config
    from infra.resource_monitor import ResourceMonitor

log = logging.getLogger("jarvis.v3.adapters")


class ResourceAdapter:
    """Resource pressure callbacks → ResourceAlert events + model switching."""

    def __init__(
        self,
        monitor: ResourceMonitor,
        agent_state: AgentState,
        llm: LLMClient,
        config: Config,
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
            alert = "Pressure cleared. Restoring the standard model." if switched else "Pressure cleared, Sir."

        log.warning(
            "Resource pressure %s: %s=%.1f -> %s",
            "entered" if event.entering else "cleared",
            event.metric, event.value, self._llm.current_model,
        )
        self._bus.publish(SpeakRequest(text=alert, priority=PRIORITY_CRITICAL))

    def snapshot(self) -> dict:
        return self._monitor.snapshot()
