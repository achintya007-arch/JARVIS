"""
Resource monitor — watches VRAM, RAM, CPU and triggers callbacks under pressure
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import psutil

try:
    import pynvml
    pynvml.nvmlInit()
    NVML_OK = True
except Exception:
    NVML_OK = False

from core.config import ResourceConfig

log = logging.getLogger("jarvis.resources")

# (metric, value, entering) — entering=True on the transition INTO pressure,
# False on the transition back to normal. Never called while state is unchanged.
PressureCallback = Callable[[str, float, bool], Awaitable[None]]


class ResourceMonitor:
    def __init__(self, config: ResourceConfig):
        self.config = config
        self._task: asyncio.Task | None = None
        self._pressure_cb: PressureCallback | None = None
        self._handle = None
        # Edge-trigger state per metric — without this, the callback fired on
        # EVERY poll tick (every poll_interval_sec) while a metric stayed above
        # threshold, spamming a "critical alert" every couple of seconds and
        # repeatedly interrupting TTS/STT for no new information.
        self._in_pressure: dict[str, bool] = {"vram_mb": False, "ram_pct": False}

        if NVML_OK:
            try:
                self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            except Exception:
                self._handle = None

    def on_pressure(self, callback: PressureCallback):
        self._pressure_cb = callback

    async def start(self):
        self._task = asyncio.create_task(self._poll_loop())
        log.info("Resource monitor started (poll every %.1fs)", self.config.poll_interval_sec)

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def _poll_loop(self):
        while True:
            await asyncio.sleep(self.config.poll_interval_sec)
            metrics = self.snapshot()
            self._log_metrics(metrics)

            await self._check_edge(
                "vram_mb", metrics.get("vram_used_mb", 0), self.config.max_vram_mb,
            )
            await self._check_edge(
                "ram_pct", metrics.get("ram_pct", 0), self.config.max_ram_pct * 100,
            )

    async def _check_edge(self, metric: str, value: float, threshold: float) -> None:
        """Fire the pressure callback only on a state transition."""
        now_over = value > threshold
        was_over = self._in_pressure.get(metric, False)
        if now_over == was_over:
            return  # no change — don't re-alert every poll tick
        self._in_pressure[metric] = now_over
        if self._pressure_cb:
            await self._pressure_cb(metric, value, now_over)

    def snapshot(self) -> dict:
        vm = psutil.virtual_memory()
        result = {
            "ram_used_gb": vm.used / 1e9,
            "ram_total_gb": vm.total / 1e9,
            "ram_pct": vm.percent,
            "cpu_pct": psutil.cpu_percent(interval=None),
        }

        if NVML_OK and self._handle:
            try:
                mem = pynvml.nvmlDeviceGetMemoryInfo(self._handle)
                result["vram_used_mb"] = mem.used / 1e6
                result["vram_total_mb"] = mem.total / 1e6
                result["vram_pct"] = mem.used / mem.total * 100
                util = pynvml.nvmlDeviceGetUtilizationRates(self._handle)
                result["gpu_util_pct"] = util.gpu
            except Exception as e:
                log.debug("NVML read failed: %s", e)

        return result

    def _log_metrics(self, m: dict):
        vram_str = (
            f"VRAM {m['vram_used_mb']:.0f}/{m['vram_total_mb']:.0f} MB  "
            if "vram_used_mb" in m else ""
        )
        log.debug(
            "RAM %.1f GB (%.0f%%)  %sCPU %.0f%%",
            m["ram_used_gb"], m["ram_pct"],
            vram_str, m["cpu_pct"]
        )
