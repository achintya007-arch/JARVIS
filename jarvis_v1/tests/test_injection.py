"""
Injection regressions for the two holes found in the desktop-tool audit:

  1. open_app: a name-only call skipped the allowlist and ran
     subprocess.Popen(name, shell=True) — {"name": "calc & whoami"} ran whoami.
  2. paste_text: text was interpolated into a PowerShell command
     (Set-Clipboard -Value '<text>'), so a ' in the text ran PowerShell.

Now every launch goes through action/app_launcher (allowlist → absolute path →
argv, shell=False) and the clipboard is set through the Win32 clipboard API.
Mocks prove no process/shell is invoked; on Windows, canary tests run the real
code and prove the payload's side effect never happens.
"""

import asyncio
import sys
import types

import pytest

pytest.importorskip("psutil")
pytest.importorskip("httpx")

from action import app_launcher
from action.agent_executor import AgentExecutor
from action.fast_router import FastRouter
from action.tools import screen_tool
from core.config import AgentConfig

INJECTIONS = [
    "calc & whoami",
    "notepad & whoami",
    "notepad.exe & whoami",
    'notepad" & whoami & "',
    "notepad' ; whoami ; '",
    "notepad; calc",
    "notepad | calc",
    "`calc`",
    "$(calc)",
    "calc\nwhoami",
    r"C:\Windows\System32\cmd.exe",
    r"..\..\Windows\System32\cmd",
    "cmd /c whoami",
    "powershell -enc AAAA",
    "https://evil.example/x.exe",
]


@pytest.fixture
def no_processes(monkeypatch):
    """Fail the test if anything tries to start a process or open a file."""
    calls = []

    def popen(*a, **k):
        calls.append(("Popen", a, k))
        raise AssertionError(f"process started: {a}")

    monkeypatch.setattr(app_launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(app_launcher.os, "startfile", lambda *a, **k: calls.append(("startfile", a)),
                        raising=False)
    return calls


async def _open_app(args):
    ex = AgentExecutor(AgentConfig(confirmation_required=False))
    [res] = await ex.execute([{"tool": "open_app", "args": args}])
    return res


# ── open_app ──────────────────────────────────────────────────────────────────

class TestOpenAppInjection:
    @pytest.mark.parametrize("payload", INJECTIONS)
    @pytest.mark.parametrize("field", ["name", "command", "app"])
    async def test_payload_is_denied_and_nothing_runs(self, payload, field, no_processes):
        res = await _open_app({field: payload})
        assert res["status"] == "denied"
        assert res["code"] == "E_NOT_ALLOWED"
        assert no_processes == []

    async def test_name_only_path_is_allowlisted_too(self, no_processes):
        # The audited hole: no "command", just a name the LLM made up.
        with pytest.raises(app_launcher.AppNotAllowed):
            await screen_tool.ScreenTool().open_app("calc & whoami")
        assert no_processes == []

    def test_denied_reason_names_the_app(self):
        with pytest.raises(app_launcher.AppNotAllowed) as e:
            app_launcher.resolve_key("calc & whoami")
        assert "calc & whoami" in str(e.value)

    def test_no_fuzzy_matching_on_untrusted_names(self):
        for near_miss in ("notepad2", "xnotepad", "note pad", "chrome --load-extension=evil"):
            with pytest.raises(app_launcher.AppNotAllowed):
                app_launcher.resolve_key(near_miss)


class TestAllowedLaunchUsesNoShell:
    @pytest.mark.parametrize("name,key", [
        ("notepad", "notepad"), ("Notepad", "notepad"), ("notepad.exe", "notepad"),
        ("VS Code", "vscode"), ("visual studio code", "vscode"), ("calc", "calculator"),
    ])
    async def test_argv_list_without_shell(self, monkeypatch, name, key):
        started = []
        monkeypatch.setattr(app_launcher, "resolve_executable", lambda spec: rf"C:\Apps\{spec.key}.exe")
        monkeypatch.setattr(app_launcher.subprocess, "Popen", lambda argv, **kw: started.append((argv, kw)))
        res = await _open_app({"name": name})
        assert res["status"] == "ok"
        [(argv, kw)] = started
        assert isinstance(argv, list) and argv[0] == rf"C:\Apps\{key}.exe"
        assert kw.get("shell") is False

    def test_fast_router_only_emits_allowlisted_keys(self):
        for _, (key, _) in FastRouter._APP_MAP.items():
            assert key in app_launcher.ALLOWED_APPS
        res = FastRouter().route("open notepad")
        assert res.args == {"app": "notepad", "name": "Notepad"}

    async def test_not_installed_is_an_error_not_a_shell_fallback(self, monkeypatch, no_processes):
        monkeypatch.setattr(app_launcher, "resolve_executable", lambda spec: None)
        res = await _open_app({"name": "word"})
        assert res["status"] == "error" and no_processes == []


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows process launch")
class TestOpenAppCanary:
    @pytest.mark.parametrize("template", [
        'notepad & echo pwned > "{canary}"',
        'calc & cmd /c "echo pwned > {canary}"',
        "notepad\" & echo pwned > \"{canary}",
        'notepad; New-Item -Path "{canary}"',
    ])
    async def test_real_launch_path_never_creates_the_canary(self, tmp_path, template):
        canary = tmp_path / "canary.txt"
        res = await _open_app({"name": template.format(canary=canary)})
        await asyncio.sleep(1.0)                      # give a (wrongly) spawned shell time to run
        assert res["status"] == "denied"
        assert not canary.exists()


# ── paste_text / copy_selection ───────────────────────────────────────────────

PASTE_PAYLOADS = [
    "'; Remove-Item -Recurse C:\\important; '",
    "it's; whoami; `calc`",
    "$(Start-Process calc)",
    "a\"b'c;d`e|f&g",
    "line1\nline2; whoami",
]


@pytest.fixture
def fake_desktop(monkeypatch):
    """pyautogui + pyperclip fakes; any subprocess call fails the test."""
    clipboard = {"text": None}
    keys = []
    monkeypatch.setitem(sys.modules, "pyperclip", types.SimpleNamespace(
        copy=lambda t: clipboard.__setitem__("text", t),
        paste=lambda: clipboard["text"] or "",
        PyperclipException=Exception,
    ))
    monkeypatch.setattr(screen_tool, "_PYAUTOGUI_OK", True)
    monkeypatch.setattr(screen_tool, "pyautogui", types.SimpleNamespace(hotkey=lambda *k: keys.append(k)),
                        raising=False)

    import subprocess

    def boom(*a, **k):
        raise AssertionError(f"subprocess used: {a}")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    return clipboard, keys


class TestPasteTextInjection:
    @pytest.mark.parametrize("payload", PASTE_PAYLOADS)
    async def test_text_is_data_never_a_command(self, payload, fake_desktop):
        clipboard, keys = fake_desktop
        out = await screen_tool.ScreenTool().paste_text(payload)
        assert clipboard["text"] == payload                 # exact, unescaped, unexecuted
        assert keys == [("ctrl", "v")]
        assert out == f"Pasted {len(payload)} characters."

    async def test_copy_selection_reads_without_powershell(self, fake_desktop):
        clipboard, keys = fake_desktop
        clipboard["text"] = "selected `text`; whoami"
        assert await screen_tool.ScreenTool().copy_selection() == "selected `text`; whoami"
        assert keys == [("ctrl", "c")]


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows clipboard")
class TestClipboardCanary:
    async def test_real_clipboard_round_trip_runs_nothing(self, tmp_path):
        pyperclip = pytest.importorskip("pyperclip")
        canary = tmp_path / "canary.txt"
        payload = f"'; New-Item -ItemType File -Path '{canary}'; '`$(whoami)"
        saved = pyperclip.paste()                           # restore your clipboard afterwards
        try:
            pyperclip.copy(payload)                         # what paste_text does before Ctrl+V
            await asyncio.sleep(0.5)
            assert pyperclip.paste() == payload
            assert not canary.exists()
        finally:
            pyperclip.copy(saved)
