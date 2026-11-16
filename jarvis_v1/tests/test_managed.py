"""The Brain under the Body launcher (core_v3/managed.py) and the launcher's
preflight report (tools/preflight.py)."""

import asyncio
import io
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config import Config
from core_v3 import managed
from core_v3.brain import Brain
from tools import preflight

ROOT = Path(__file__).resolve().parents[1]


class TestApply:
    def test_forces_bbp_on_and_takes_the_bodys_port(self):
        config = Config()
        assert config.bbp.enabled is False
        managed.apply(config, {"JARVIS_MANAGED": "1", "JARVIS_BBP_PORT": "8799"})
        assert (config.managed, config.bbp.enabled, config.bbp.port) == (True, True, 8799)

    @pytest.mark.parametrize("port", ["abc", "0", "70000"])
    def test_a_bad_port_keeps_the_configured_one(self, port):
        config = Config()
        managed.apply(config, {"JARVIS_BBP_PORT": port})
        assert config.bbp.port == 8765 and config.bbp.enabled

    def test_only_exactly_1_means_managed(self):
        assert managed.is_managed({"JARVIS_MANAGED": "1"})
        assert not managed.is_managed({"JARVIS_MANAGED": "0"})
        assert not managed.is_managed({})

    def test_jarvis_mode_wake_is_accepted(self, monkeypatch, tmp_path):
        (tmp_path / "config.yaml").write_text("mode: text\n")
        monkeypatch.setenv("JARVIS_MODE", "wake")
        assert Config.load(tmp_path / "config.yaml").mode == "wake"


class TestStdinWatch:
    async def _watch(self, stream):
        fired = []
        loop = asyncio.get_running_loop()
        thread = managed.watch_stdin(loop, lambda: fired.append(True), stream)
        thread.join(2)
        await asyncio.sleep(0.05)               # let call_soon_threadsafe run
        return fired

    async def test_shutdown_line_stops_once(self):
        assert await self._watch(io.StringIO("hello\n  SHUTDOWN \nshutdown\n")) == [True]

    async def test_the_launcher_going_away_stops_it(self):
        assert await self._watch(io.StringIO("")) == [True]

    def test_a_real_child_shuts_down_cleanly_when_told(self):
        # The same mechanism as the Brain: the main task is cancelled, cleanup runs.
        script = textwrap.dedent("""
            import asyncio
            from core_v3 import managed
            async def main():
                managed.watch_stdin(asyncio.get_running_loop(), asyncio.current_task().cancel)
                print("ready", flush=True)
                try:
                    await asyncio.Event().wait()
                finally:
                    print("cleaned up", flush=True)
            try:
                asyncio.run(main())
            except asyncio.CancelledError:
                print("stopped", flush=True)
        """)
        child = subprocess.Popen([sys.executable, "-c", script], cwd=ROOT, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True)
        assert child.stdout.readline().strip() == "ready"
        start = time.monotonic()
        child.stdin.write("shutdown\n")
        child.stdin.flush()
        out, _ = child.communicate(timeout=10)
        assert out.split() == ["cleaned", "up", "stopped"]
        assert child.returncode == 0 and time.monotonic() - start < 5

    def test_stdin_is_free_while_the_pipe_is_watched(self):
        # Regression: with the watcher's read pending on stdin itself, loading
        # scipy (its Fortran runtime queries the stdin handle) hung the Brain
        # forever on Windows. Now stdin is NUL and only the watcher has the pipe.
        script = textwrap.dedent("""
            import asyncio, os, subprocess, sys
            from core_v3 import managed
            async def main():
                managed.watch_stdin(asyncio.get_running_loop(), asyncio.current_task().cancel)
                await asyncio.sleep(0.2)                     # the read is pending now
                checks = [repr(sys.stdin.read()), repr(os.read(0, 10))]
                if os.name == "nt":
                    import ctypes
                    k32 = ctypes.windll.kernel32
                    k32.GetStdHandle.restype = ctypes.c_void_p
                    k32.GetFileType.argtypes = [ctypes.c_void_p]
                    checks.append(str(k32.GetFileType(k32.GetStdHandle(-10))))   # 2: a char device (NUL)
                grandchild = subprocess.run([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"],
                                            capture_output=True, text=True, timeout=20)
                checks.append(grandchild.stdout.strip())
                print(" ".join(checks), flush=True)
                await asyncio.Event().wait()
            try:
                asyncio.run(main())
            except asyncio.CancelledError:
                print("stopped", flush=True)
        """)
        child = subprocess.Popen([sys.executable, "-c", script], cwd=ROOT, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True)
        try:
            line = child.stdout.readline().split()
            child.stdin.write("shutdown\n")
            child.stdin.flush()
            out, _ = child.communicate(timeout=20)
        finally:
            child.kill()
        expected = ["''", "b''"] + (["2"] if os.name == "nt" else []) + ["''"]
        assert line == expected
        assert out.split() == ["stopped"]


class TestBrainUnderTheLauncher:
    async def test_text_mode_never_reads_the_console(self):
        console = SimpleNamespace(readline=None)            # would fail if called
        fake = SimpleNamespace(_managed=lambda: True, _console=console, _stopped=False)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(Brain._text_loop(fake), 0.2)

    async def test_confirmations_skip_the_console(self):
        seen = {}

        class Broker:
            async def ask(self, request, channels):
                seen.update(channels)
                return SimpleNamespace(approved=False, by="timeout", confirm_id="c")

        for is_managed, expected in ((True, set()), (False, {"text"})):
            seen.clear()
            fake = SimpleNamespace(_text_mode=lambda: True, _managed=lambda m=is_managed: m,
                                   _confirmations=Broker(), _orchestrator=None, _console_answer=object())
            await Brain._confirm_request(fake, SimpleNamespace(prompt="?", detail="", action=None))
            assert set(seen) == expected


class TestPreflight:
    def test_report(self, monkeypatch):
        config = Config()
        monkeypatch.setattr(preflight.importlib.util, "find_spec", lambda name: None)
        r = preflight.report(config)
        assert r["models"] == {"required": ["qwen2.5:7b-instruct-q4_K_M", "nomic-embed-text"],
                               "optional": ["qwen2.5:3b-instruct-q4_K_M"]}
        assert r["bbp"] == {"enabled": False, "port": 8765}
        assert r["phonebox"] == {"enabled": False, "url": "http://127.0.0.1:8780"}
        assert r["mode"] == "wake" and "voice packages missing" in r["warnings"][0]

    def test_text_mode_needs_no_voice_packages(self, monkeypatch):
        config = Config()
        config.mode = "text"
        monkeypatch.setattr(preflight.importlib.util, "find_spec", lambda name: None)
        assert preflight.report(config)["warnings"] == []

    def test_the_command_prints_one_json_line(self, tmp_path):
        (tmp_path / "config.yaml").write_text("llm:\n  model: m1\n  fallback_model: m1\nbbp:\n  port: 9001\n")
        out = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                              "from tools.preflight import main; sys.exit(main())", str(ROOT)],
                             cwd=tmp_path, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        r = json.loads(out.stdout)
        assert r["models"] == {"required": ["m1", "nomic-embed-text"], "optional": []}
        assert r["bbp"]["port"] == 9001

    def test_a_broken_config_is_an_error_line(self, tmp_path):
        (tmp_path / "config.yaml").write_text("llm: [unclosed\n")
        out = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                              "from tools.preflight import main; sys.exit(main())", str(ROOT)],
                             cwd=tmp_path, capture_output=True, text=True, timeout=60)
        assert out.returncode == 1 and "can't be read" in json.loads(out.stdout)["error"]
