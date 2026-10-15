"""
Desktop-control benchmark: success rate and latency of each desktop tool against
real apps (Notepad, VS Code, Chrome, File Explorer).

Every result is checked independently with Win32 calls, not by trusting the
tool's own message: a launch counts only if a new window of that app actually
appears, a focus only if that window is actually in the foreground, a volume
change only if the endpoint volume actually reads back the new level.

    python -m tools.desktop_bench                       # current Python tools
    python -m tools.desktop_bench --backend body        # through the C# Body (BBP)
    python -m tools.desktop_bench --n 20 --only focus,list

What it does to your desktop, and undoes: opens app windows (closes only the
windows it opened), moves focus between them (restores your foreground window
at the end), nudges the volume by 1–2 % (restores it), and takes screenshots
(deletes them). Results: printed table + JSON under data/bench/.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import statistics
import sys
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

if sys.platform != "win32":
    raise SystemExit("desktop_bench is Windows-only")

import psutil

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
WM_CLOSE = 0x0010
DWMWA_CLOAKED = 14
_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


# ── Ground truth (Win32, independent of the tools under test) ─────────────────

@dataclass(frozen=True)
class Win:
    hwnd: int
    title: str
    cls: str
    process: str


def _title(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _cloaked(hwnd) -> bool:
    val = ctypes.c_int(0)
    dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val))
    return val.value != 0


def _process(hwnd) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        return psutil.Process(pid.value).name().lower()
    except psutil.Error:
        return "?"


def real_windows() -> list[Win]:
    """What a person would call 'open windows': visible, titled, not cloaked."""
    out: list[Win] = []

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not _cloaked(hwnd):
            t = _title(hwnd)
            if t.strip():
                out.append(Win(int(hwnd), t, _class(hwnd), _process(hwnd)))
        return True

    user32.EnumWindows(_EnumProc(cb), 0)
    return out


def foreground() -> Win | None:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    return Win(int(hwnd), _title(hwnd), _class(hwnd), _process(hwnd))


# ── Target apps ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class App:
    key: str
    process: str
    cls: str
    executor_args: dict          # what FastRouter / the LLM sends today

    def owns(self, w: Win) -> bool:
        return w.process == self.process and w.cls == self.cls


APPS = {
    "notepad": App("notepad", "notepad.exe", "Notepad", {"command": "notepad", "name": "Notepad"}),
    "chrome": App("chrome", "chrome.exe", "Chrome_WidgetWin_1", {"command": "start chrome", "name": "Chrome"}),
    "vscode": App("vscode", "code.exe", "Chrome_WidgetWin_1", {"command": "code", "name": "VS Code"}),
    "explorer": App("explorer", "explorer.exe", "CabinetWClass", {"name": "explorer"}),
}


# ── Backends: the thing being measured ────────────────────────────────────────

class PythonBackend:
    """Today's tools, called exactly as JARVIS calls them (AgentExecutor)."""
    name = "python"

    def __init__(self) -> None:
        from action.agent_executor import AgentExecutor
        from core.config import Config
        self._ex = AgentExecutor(config=Config().agent)

    async def _call(self, tool: str, args: dict):
        [res] = await self._ex.execute([{"tool": tool, "args": args}])
        if res["status"] != "ok":
            raise RuntimeError(res.get("error") or res.get("reason"))
        return res["result"]

    async def launch(self, app: App) -> str:
        return str(await self._call("open_app", app.executor_args))

    async def focus(self, title: str) -> str:
        return str(await self._call("focus_window", {"title": title}))

    async def list_windows(self) -> list[str]:
        out = await self._call("list_windows", {})
        return [] if out == "No open windows found." else out.split("\n")

    async def set_volume(self, level: int) -> str:
        return str(await self._call("set_volume", {"level": level}))

    async def read_screen(self) -> dict:
        """No structured screen reader exists today: the closest tool is a
        screenshot, which yields a file path, not content."""
        path = Path(await self._call("screenshot", {}))
        ok = path.is_file() and path.stat().st_size > 0
        path.unlink(missing_ok=True)                       # don't keep pictures of your screen
        return {"ok": ok, "nodes": 0, "note": "screenshot path only, no content"}

    async def close(self) -> None:
        await self._ex.shutdown()


def make_backend(name: str):
    if name == "python":
        return PythonBackend()
    if name == "body":
        from tools.desktop_bench_body import BodyBackend  # added with the C# actions
        return BodyBackend()
    raise SystemExit(f"unknown backend {name!r}")


# ── Measurements ──────────────────────────────────────────────────────────────

@dataclass
class Sample:
    ok: bool
    call_ms: float               # until the tool returned
    effect_ms: float | None      # until the effect was verified (None = never)
    detail: str = ""


@dataclass
class Result:
    tool: str
    target: str
    samples: list[Sample] = field(default_factory=list)

    def summary(self) -> dict:
        oks = [s for s in self.samples if s.ok]
        eff = [s.effect_ms for s in oks if s.effect_ms is not None]
        return {
            "tool": self.tool, "target": self.target, "n": len(self.samples),
            "success": len(oks), "rate": len(oks) / len(self.samples) if self.samples else 0.0,
            "median_ms": round(statistics.median(eff), 1) if eff else None,
            "p95_ms": round(_p95(eff), 1) if eff else None,
            "failures": sorted({s.detail for s in self.samples if not s.ok and s.detail})[:4],
        }


def _p95(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]


async def _poll(cond, timeout: float, step: float = 0.02):
    """Wait for cond() to return something truthy; (value, seconds) or (None, None)."""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        v = cond()
        if v:
            return v, time.perf_counter() - t0
        await asyncio.sleep(step)
    return None, None


async def _close_windows(hwnds: set[int]) -> None:
    for h in hwnds:
        user32.PostMessageW(wintypes.HWND(h), WM_CLOSE, 0, 0)
    await _poll(lambda: not any(user32.IsWindow(wintypes.HWND(h)) for h in hwnds), 8.0)


async def bench_launch(backend, app: App, n: int, timeout: float) -> Result:
    r = Result("app.launch", app.key)
    for _ in range(n):
        before = {w.hwnd for w in real_windows() if app.owns(w)}
        t0 = time.perf_counter()
        try:
            msg = await backend.launch(app)
        except Exception as e:
            r.samples.append(Sample(False, (time.perf_counter() - t0) * 1000, None, f"raised: {e}"[:120]))
            continue
        call = (time.perf_counter() - t0) * 1000

        def new_window(app=app, before=before):
            return [w for w in real_windows() if app.owns(w) and w.hwnd not in before]

        found, _ = await _poll(new_window, timeout)
        effect = (time.perf_counter() - t0) * 1000 if found else None
        if found:
            r.samples.append(Sample(True, call, effect))
            await _close_windows({w.hwnd for w in found})
        else:
            fg = foreground()
            reused = fg is not None and app.owns(fg)
            r.samples.append(Sample(False, call, None,
                                    "no new window" + (" (existing window focused)" if reused else "")
                                    + f"; tool said: {msg[:60]}"))
        await asyncio.sleep(0.3)
    return r


async def _open_targets(apps: list[App]) -> dict[str, Win]:
    """One fresh window per app to focus between. Returns {app_key: Win}; an app
    whose window didn't appear is left out (never one of your windows)."""
    import subprocess

    from action.app_launcher import ALLOWED_APPS, resolve_executable
    # Windows whose titles the bench controls (never your own documents).
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    BENCH_FILE.write_text("JARVIS desktop bench target. Safe to close.", encoding="utf-8")
    setup_args = {"notepad": [str(BENCH_FILE)], "chrome": ["--new-window"],
                  "vscode": ["-n"], "explorer": [str(BENCH_DIR)]}
    targets: dict[str, Win] = {}
    for app in apps:
        before = {w.hwnd for w in real_windows() if app.owns(w)}
        exe = resolve_executable(ALLOWED_APPS[app.key])
        if exe is None:
            print(f"  ({app.key} not installed; skipping it for focus/list)", flush=True)
            continue
        subprocess.Popen([exe, *setup_args[app.key]])         # setup only, not measured
        found, _ = await _poll(
            lambda app=app, before=before: [w for w in real_windows() if app.owns(w) and w.hwnd not in before], 15)
        if found:
            targets[app.key] = found[0]
        else:
            print(f"  (no fresh {app.key} window appeared; skipping it for focus/list)", flush=True)
    await asyncio.sleep(1.0)                                   # let titles settle ("New Tab - …")
    return {k: Win(w.hwnd, _title(w.hwnd), w.cls, w.process) for k, w in targets.items()
            if user32.IsWindow(wintypes.HWND(w.hwnd))}


async def bench_focus(backend, targets: dict[str, Win], n: int) -> list[Result]:
    results = {k: Result("window.focus", k) for k in targets}
    keys = list(targets)
    for i in range(n * len(keys)):
        key = keys[i % len(keys)]
        target = Win(targets[key].hwnd, _title(targets[key].hwnd), targets[key].cls, targets[key].process)
        if (user32.GetForegroundWindow() or 0) == target.hwnd:
            # Already in front (the previous focus failed): proves nothing.
            results[key].already = getattr(results[key], "already", 0) + 1   # type: ignore[attr-defined]
            continue
        t0 = time.perf_counter()
        try:
            msg = await backend.focus(target.title)
        except Exception as e:
            results[key].samples.append(Sample(False, (time.perf_counter() - t0) * 1000, None, f"raised: {e}"[:120]))
            continue
        call = (time.perf_counter() - t0) * 1000

        def focused(target=target):
            fg = foreground()
            return fg is not None and fg.process == target.process and target.title in fg.title

        ok, _ = await _poll(focused, 1.0)
        effect = (time.perf_counter() - t0) * 1000 if ok else None
        detail = "" if ok else f"foreground is {_describe(foreground())}; tool said: {_redact(msg, target.title)}"
        results[key].samples.append(Sample(bool(ok), call, effect, detail))
        await asyncio.sleep(0.15)
    return list(results.values())


def _describe(w: Win | None) -> str:
    """Process name only: window titles can contain your documents' text."""
    return "nothing" if w is None else w.process


def _redact(msg: str, title: str) -> str:
    return msg.replace(title, "<title>")[:70]


async def bench_list(backend, targets: dict[str, Win], n: int) -> Result:
    r = Result("window.list", "all")
    for _ in range(n):
        truth = {w.title for w in real_windows()}
        t0 = time.perf_counter()
        try:
            listed = set(await backend.list_windows())
        except Exception as e:
            r.samples.append(Sample(False, (time.perf_counter() - t0) * 1000, None, f"raised: {e}"[:120]))
            continue
        ms = (time.perf_counter() - t0) * 1000
        wanted = {_title(t.hwnd) for t in targets.values()}
        missing = wanted - listed
        extra = listed - truth                                  # ghosts: hidden/cloaked/tool windows
        ok = not missing
        detail = (f"missing {len(missing)} target window(s)" if missing else "")
        r.samples.append(Sample(ok, ms, ms if ok else None, detail))
        r.extra = getattr(r, "extra", []) + [len(extra)]        # type: ignore[attr-defined]
        r.listed = getattr(r, "listed", []) + [len(listed)]     # type: ignore[attr-defined]
        r.truth = getattr(r, "truth", []) + [len(truth)]        # type: ignore[attr-defined]
    return r


def _volume_endpoint():
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetSpeakers().EndpointVolume


async def bench_volume(backend, n: int) -> Result:
    r = Result("audio.set_volume", "default")
    vol = _volume_endpoint()
    original = round(vol.GetMasterVolumeLevelScalar() * 100)
    sign = -1 if original > 5 else 1
    try:
        for i in range(n):
            level = original + sign * (1 + i % 2)               # ±1–2 %: inaudible
            t0 = time.perf_counter()
            try:
                msg = await backend.set_volume(level)
            except Exception as e:
                r.samples.append(Sample(False, (time.perf_counter() - t0) * 1000, None, f"raised: {e}"[:120]))
                continue
            call = (time.perf_counter() - t0) * 1000
            got, _ = await _poll(lambda lv=level: round(vol.GetMasterVolumeLevelScalar() * 100) == lv, 1.0)
            effect = (time.perf_counter() - t0) * 1000 if got else None
            r.samples.append(Sample(bool(got), call, effect, "" if got else f"reads back "
                                    f"{round(vol.GetMasterVolumeLevelScalar() * 100)}; tool said: {msg[:50]}"))
    finally:
        vol.SetMasterVolumeLevelScalar(original / 100.0, None)
    return r


async def bench_read(backend, n: int) -> Result:
    r = Result("uia.read", "foreground")
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            out = await backend.read_screen()
        except Exception as e:
            r.samples.append(Sample(False, (time.perf_counter() - t0) * 1000, None, f"raised: {e}"[:120]))
            continue
        ms = (time.perf_counter() - t0) * 1000
        r.samples.append(Sample(bool(out.get("ok")), ms, ms, out.get("note", "")))
    r.note = out.get("note", "") if r.samples else ""          # type: ignore[attr-defined]
    return r


# ── Runner ────────────────────────────────────────────────────────────────────

async def run(args) -> list[Result]:
    backend = make_backend(args.backend)
    apps = [APPS[a] for a in args.apps.split(",")]
    only = set(args.only.split(","))
    original_fg = user32.GetForegroundWindow()
    results: list[Result] = []
    targets: dict[str, Win] = {}
    try:
        if "launch" in only:
            for app in apps:
                print(f"launch {app.key} ×{args.launch_n} …", flush=True)
                results.append(await bench_launch(backend, app, args.launch_n, args.timeout))
        if only & {"focus", "list"}:
            targets = await _open_targets(apps)
            print(f"focus/list targets: {sorted(targets)}", flush=True)
        if "focus" in only:
            print(f"focus ×{args.n} per app …", flush=True)
            results.extend(await bench_focus(backend, targets, args.n))
        if "list" in only:
            print(f"list ×{args.n} …", flush=True)
            results.append(await bench_list(backend, targets, args.n))
        if "volume" in only:
            print(f"volume ×{args.n} …", flush=True)
            results.append(await bench_volume(backend, args.n))
        if "read" in only:
            print(f"read ×{args.n} …", flush=True)
            results.append(await bench_read(backend, args.n))
    finally:
        # Close only the windows we opened for focus/list (existing ones stay).
        opened = {w.hwnd for w in targets.values()}
        await _close_windows({h for h in opened if h not in _preexisting})
        user32.SetForegroundWindow(original_fg)
        await backend.close()
    return results


_preexisting: set[int] = set()
BENCH_DIR = Path("data/bench").resolve()
BENCH_FILE = BENCH_DIR / "jarvis_bench_target.txt"


def report(results: list[Result], backend: str) -> dict:
    rows = [r.summary() for r in results]
    for r, row in zip(results, rows, strict=True):
        if hasattr(r, "extra"):
            row["ghost_entries_median"] = statistics.median(r.extra)
            row["listed_median"] = statistics.median(r.listed)
            row["real_windows_median"] = statistics.median(r.truth)
        if hasattr(r, "note") and r.note:
            row["note"] = r.note
        if getattr(r, "already", 0):
            row["skipped_already_foreground"] = r.already
            row["failures"] = [f"{r.already} skipped: already in front"] + row["failures"]
    print(f"\n{'tool':18} {'target':10} {'success':>9} {'median':>9} {'p95':>9}  notes")
    for row in rows:
        med = f"{row['median_ms']:.0f} ms" if row["median_ms"] is not None else "—"
        p95 = f"{row['p95_ms']:.0f} ms" if row["p95_ms"] is not None else "—"
        notes = "; ".join(row["failures"])[:90]
        if "ghost_entries_median" in row:
            notes = (f"lists {row['listed_median']:.0f}, real {row['real_windows_median']:.0f}, "
                     f"ghosts {row['ghost_entries_median']:.0f}. ") + notes
        if row.get("note"):
            notes = row["note"] + ". " + notes
        print(f"{row['tool']:18} {row['target']:10} {row['success']:>3}/{row['n']:<3}({row['rate']:4.0%})"
              f" {med:>9} {p95:>9}  {notes}")
    out_dir = Path("data/bench")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"desktop_{backend}_{datetime.now():%Y%m%d_%H%M%S}.json"
    payload = {"backend": backend, "when": datetime.now().isoformat(timespec="seconds"),
               "summary": rows, "samples": {f"{r.tool}:{r.target}": [asdict(s) for s in r.samples]
                                            for r in results}}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nsaved {path}")
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", default="python", choices=["python", "body"])
    ap.add_argument("--n", type=int, default=10, help="runs per tool (focus: per app)")
    ap.add_argument("--launch-n", type=int, default=5, help="launches per app")
    ap.add_argument("--apps", default="notepad,chrome,vscode,explorer")
    ap.add_argument("--only", default="launch,focus,list,volume,read")
    ap.add_argument("--timeout", type=float, default=15.0, help="seconds to wait for a launched window")
    args = ap.parse_args()
    _preexisting.update(w.hwnd for w in real_windows())
    report(asyncio.run(run(args)), args.backend)
    return 0


if __name__ == "__main__":
    sys.exit(main())
