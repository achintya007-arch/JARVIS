"""
ScreenTool — screen capture, window management, mouse/keyboard automation.

Wraps pyautogui + pygetwindow into async-safe methods.
All blocking calls run in asyncio.to_thread() — event loop never stalls.

Tools exposed to the LLM:
  screenshot        — capture screen, return description path for vision model
  click             — click at (x, y) or on text found via locateOnScreen
  type_text         — type a string at current cursor position
  hotkey            — send a keyboard shortcut e.g. ctrl+c
  open_app          — launch an application by name
  focus_window      — bring a window to foreground by title substring
  list_windows      — list all open window titles
  move_mouse        — move mouse to (x, y) without clicking
  scroll            — scroll up/down at current position
  get_screen_size   — return screen resolution
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger("jarvis.screen")

# ── Optional imports — graceful degradation if not installed ──────────────────
try:
    import pyautogui
    pyautogui.FAILSAFE = True       # move mouse to top-left corner to abort
    pyautogui.PAUSE = 0.05          # small delay between actions — more reliable
    _PYAUTOGUI_OK = True
except ImportError:
    _PYAUTOGUI_OK = False
    log.warning("pyautogui not installed — screen control disabled. pip install pyautogui")

try:
    import pygetwindow as gw
    _GW_OK = True
except ImportError:
    _GW_OK = False
    log.warning("pygetwindow not installed — window management disabled. pip install pygetwindow")

try:
    from PIL import ImageGrab
    _PIL_OK = True
except ImportError:
    _PIL_OK = False
    log.warning("Pillow not installed — screenshot disabled. pip install pillow")


def active_window_title() -> str:
    """
    Return the title of the foreground window, or "" if unavailable.
    Cheap, synchronous, and safe on headless systems (returns "").
    Used to give JARVIS ambient awareness of what the user is doing.
    """
    if not _GW_OK:
        return ""
    try:
        w = gw.getActiveWindow()
        return (w.title or "").strip() if w else ""
    except Exception:
        return ""


# ── Known application paths (Windows) ─────────────────────────────────────────
_APP_MAP: dict[str, str] = {
    "chrome":      "chrome",
    "firefox":     "firefox",
    "notepad":     "notepad",
    "explorer":    "explorer",
    "calculator":  "calc",
    "cmd":         "cmd",
    "powershell":  "powershell",
    "vscode":      "code",
    "code":        "code",
    "spotify":     r"%APPDATA%\Spotify\Spotify.exe",
    "discord":     r"%LOCALAPPDATA%\Discord\Update.exe --processStart Discord.exe",
    "task manager":"taskmgr",
    "paint":       "mspaint",
    "word":        "winword",
    "excel":       "excel",
    "outlook":     "outlook",
}

# ── Screenshot save dir ────────────────────────────────────────────────────────
_SCREENSHOT_DIR = Path("data/screenshots")


class ScreenTool:
    """
    Async-safe screen automation. Every public method is a coroutine.
    Instantiate once and inject into AgentExecutor.
    """

    def __init__(self, screenshot_dir: str = "data/screenshots"):
        self._dir = Path(screenshot_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._counter = 0

    # ── Availability check ────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return _PYAUTOGUI_OK

    def _require(self) -> None:
        if not _PYAUTOGUI_OK:
            raise RuntimeError(
                "pyautogui is not installed. Run: pip install pyautogui pygetwindow pillow"
            )

    # ── Screenshot ────────────────────────────────────────────────────────────

    async def screenshot(self, region: Optional[tuple[int, int, int, int]] = None) -> str:
        """
        Capture the screen (or a region) and save to disk.
        Returns the saved file path as a string — pass to vision model.

        region: (left, top, width, height) or None for full screen.
        """
        self._require()

        def _capture() -> str:
            self._counter += 1
            path = self._dir / f"screen_{self._counter:04d}.png"
            if _PIL_OK and region is None:
                img = ImageGrab.grab()
                img.save(str(path))
            elif region:
                pyautogui.screenshot(str(path), region=region)
            else:
                pyautogui.screenshot(str(path))
            log.debug("Screenshot saved: %s", path)
            return str(path)

        return await asyncio.to_thread(_capture)

    # ── Mouse ─────────────────────────────────────────────────────────────────

    async def click(
        self,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
        clicks: int = 1,
    ) -> str:
        """Click at (x, y). Omit coords to click at current position."""
        self._require()

        def _click() -> str:
            if x is not None and y is not None:
                pyautogui.click(x, y, button=button, clicks=clicks)
                return f"Clicked ({x}, {y}) with {button} button."
            else:
                pyautogui.click(button=button, clicks=clicks)
                pos = pyautogui.position()
                return f"Clicked at current position ({pos.x}, {pos.y})."

        return await asyncio.to_thread(_click)

    async def double_click(self, x: int, y: int) -> str:
        self._require()
        await asyncio.to_thread(pyautogui.doubleClick, x, y)
        return f"Double-clicked ({x}, {y})."

    async def right_click(self, x: int, y: int) -> str:
        self._require()
        await asyncio.to_thread(pyautogui.rightClick, x, y)
        return f"Right-clicked ({x}, {y})."

    async def move_mouse(self, x: int, y: int, duration: float = 0.2) -> str:
        """Move mouse without clicking."""
        self._require()
        await asyncio.to_thread(pyautogui.moveTo, x, y, duration=duration)
        return f"Mouse moved to ({x}, {y})."

    async def scroll(self, clicks: int, x: Optional[int] = None, y: Optional[int] = None) -> str:
        """
        Scroll at (x, y) or current position.
        Positive clicks = scroll up, negative = scroll down.
        """
        self._require()

        def _scroll() -> str:
            if x is not None and y is not None:
                pyautogui.scroll(clicks, x=x, y=y)
            else:
                pyautogui.scroll(clicks)
            direction = "up" if clicks > 0 else "down"
            return f"Scrolled {direction} {abs(clicks)} clicks."

        return await asyncio.to_thread(_scroll)

    async def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3) -> str:
        """Click and drag from (x1,y1) to (x2,y2)."""
        self._require()
        await asyncio.to_thread(pyautogui.drag, x2 - x1, y2 - y1, duration=duration, button="left")
        return f"Dragged from ({x1},{y1}) to ({x2},{y2})."

    # ── Keyboard ──────────────────────────────────────────────────────────────

    async def type_text(self, text: str, interval: float = 0.04) -> str:
        """
        Type text at the current cursor position.
        Uses typewrite for printable ASCII, write for everything else.
        """
        self._require()

        def _type() -> str:
            # typewrite is more reliable for automation; write handles unicode
            try:
                pyautogui.typewrite(text, interval=interval)
            except Exception:
                pyautogui.write(text, interval=interval)
            return f"Typed {len(text)} characters."

        return await asyncio.to_thread(_type)

    async def hotkey(self, *keys: str) -> str:
        """
        Press a keyboard shortcut.
        Examples: hotkey('ctrl','c')  hotkey('alt','f4')  hotkey('win','d')
        """
        self._require()
        await asyncio.to_thread(pyautogui.hotkey, *keys)
        combo = "+".join(keys)
        return f"Pressed {combo}."

    async def press_key(self, key: str, presses: int = 1) -> str:
        """Press a single key one or more times. e.g. 'enter', 'esc', 'tab'."""
        self._require()
        await asyncio.to_thread(pyautogui.press, key, presses=presses)
        return f"Pressed '{key}' {presses} time(s)."

    async def key_down(self, key: str) -> str:
        self._require()
        await asyncio.to_thread(pyautogui.keyDown, key)
        return f"Holding key: {key}."

    async def key_up(self, key: str) -> str:
        self._require()
        await asyncio.to_thread(pyautogui.keyUp, key)
        return f"Released key: {key}."

    # ── Screen info ───────────────────────────────────────────────────────────

    async def get_screen_size(self) -> str:
        self._require()
        size = await asyncio.to_thread(pyautogui.size)
        return f"Screen resolution: {size.width}x{size.height}."

    async def get_mouse_position(self) -> str:
        self._require()
        pos = await asyncio.to_thread(pyautogui.position)
        return f"Mouse at ({pos.x}, {pos.y})."

    # ── Window management ─────────────────────────────────────────────────────

    async def list_windows(self) -> list[str]:
        """Return all visible window titles."""
        if not _GW_OK:
            raise RuntimeError("pygetwindow not installed. Run: pip install pygetwindow")

        def _list() -> list[str]:
            return sorted(
                set(w.title for w in gw.getAllWindows() if w.title.strip()),
                key=str.lower,
            )

        return await asyncio.to_thread(_list)

    async def focus_window(self, title: str) -> str:
        """
        Bring a window to the foreground by partial title match.
        e.g. focus_window('Chrome')  focus_window('Spotify')
        """
        if not _GW_OK:
            raise RuntimeError("pygetwindow not installed.")

        def _focus() -> str:
            matches = [w for w in gw.getAllWindows() if title.lower() in w.title.lower()]
            if not matches:
                return f"No window found matching '{title}'."
            win = matches[0]
            try:
                win.restore()
                win.activate()
                return f"Focused: {win.title}"
            except Exception as e:
                return f"Could not focus '{win.title}': {e}"

        return await asyncio.to_thread(_focus)

    async def minimize_window(self, title: str) -> str:
        if not _GW_OK:
            raise RuntimeError("pygetwindow not installed.")

        def _min() -> str:
            matches = [w for w in gw.getAllWindows() if title.lower() in w.title.lower()]
            if not matches:
                return f"No window found matching '{title}'."
            matches[0].minimize()
            return f"Minimized: {matches[0].title}"

        return await asyncio.to_thread(_min)

    async def maximize_window(self, title: str) -> str:
        if not _GW_OK:
            raise RuntimeError("pygetwindow not installed.")

        def _max() -> str:
            matches = [w for w in gw.getAllWindows() if title.lower() in w.title.lower()]
            if not matches:
                return f"No window found matching '{title}'."
            matches[0].maximize()
            return f"Maximized: {matches[0].title}"

        return await asyncio.to_thread(_max)

    async def close_window(self, title: str) -> str:
        if not _GW_OK:
            raise RuntimeError("pygetwindow not installed.")

        def _close() -> str:
            matches = [w for w in gw.getAllWindows() if title.lower() in w.title.lower()]
            if not matches:
                return f"No window found matching '{title}'."
            matches[0].close()
            return f"Closed: {matches[0].title}"

        return await asyncio.to_thread(_close)

    # ── App launcher ──────────────────────────────────────────────────────────

    async def open_app(self, name: str) -> str:
        """
        Launch an application by common name or full path.
        Falls back to os.startfile if not in the known map.
        """
        import subprocess

        def _open() -> str:
            key = name.lower().strip()
            cmd = _APP_MAP.get(key, name)
            # Expand environment variables (e.g. %APPDATA%)
            cmd = os.path.expandvars(cmd)
            try:
                subprocess.Popen(cmd, shell=True)
                return f"Launched {name}."
            except Exception as e:
                return f"Could not launch {name}: {e}"

        return await asyncio.to_thread(_open)

    async def close_app(self, name: str) -> str:
        """Kill a process by name. e.g. close_app('notepad.exe')"""
        import subprocess

        def _close() -> str:
            exe = name if name.endswith(".exe") else f"{name}.exe"
            result = subprocess.run(
                ["taskkill", "/f", "/im", exe],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                return f"Closed {name}."
            return f"Could not close {name}: {result.stderr.strip()}"

        return await asyncio.to_thread(_close)

    # ── Clipboard ─────────────────────────────────────────────────────────────

    async def copy_selection(self) -> str:
        """Copy current selection to clipboard and return its text."""
        self._require()

        def _copy() -> str:
            pyautogui.hotkey("ctrl", "c")
            import time; time.sleep(0.15)
            try:
                import subprocess
                result = subprocess.run(
                    ["powershell", "-command", "Get-Clipboard"],
                    capture_output=True, text=True, timeout=3
                )
                return result.stdout.strip()[:2000] or "(clipboard empty)"
            except Exception:
                return "(clipboard contents unavailable)"

        return await asyncio.to_thread(_copy)

    async def paste_text(self, text: str) -> str:
        """Set clipboard content and paste it."""
        self._require()

        def _paste() -> str:
            try:
                import subprocess
                subprocess.run(
                    ["powershell", "-command", f"Set-Clipboard -Value '{text}'"],
                    capture_output=True, timeout=3
                )
            except Exception:
                pass
            pyautogui.hotkey("ctrl", "v")
            return f"Pasted {len(text)} characters."

        return await asyncio.to_thread(_paste)