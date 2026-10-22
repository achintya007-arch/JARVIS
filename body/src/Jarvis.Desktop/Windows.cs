using System.Diagnostics;
using System.Runtime.InteropServices;

namespace Jarvis.Desktop;

/// <summary>A real top-level window: what Alt+Tab would show.</summary>
public sealed record WindowRef(IntPtr Hwnd, string Title, string Process);

public interface IWindowSystem
{
    /// <summary>Real windows in z-order (most recently active first), excluding the Body's own.</summary>
    IReadOnlyList<WindowRef> List();

    /// <summary>The foreground window, or — when that is the Body's own HUD — the
    /// next real window behind it (the app you were using before you typed).</summary>
    WindowRef? ForegroundExcludingSelf();

    /// <summary>Bring a window to the front; returns the strategy that worked.
    /// Throws InvalidOperationException listing what was tried if Windows refuses.</summary>
    string Focus(IntPtr hwnd);
}

public sealed class Win32WindowSystem : IWindowSystem
{
    private static readonly int SelfPid = Environment.ProcessId;

    public IReadOnlyList<WindowRef> List()
    {
        var found = new List<WindowRef>();
        Native.EnumWindows((hwnd, _) =>
        {
            if (IsReal(hwnd) && ProcessOf(hwnd) is { } proc && proc.Pid != SelfPid)
            {
                var title = Native.WindowText(hwnd);
                if (title.Trim().Length > 0)
                    found.Add(new WindowRef(hwnd, title, proc.Name));
            }
            return true;
        }, IntPtr.Zero);
        return found;
    }

    public WindowRef? ForegroundExcludingSelf()
    {
        var fg = Native.GetForegroundWindow();
        if (fg != IntPtr.Zero && ProcessOf(fg) is { } proc && proc.Pid != SelfPid && IsReal(fg))
            return new WindowRef(fg, Native.WindowText(fg), proc.Name);
        return List().FirstOrDefault();       // z-order: the window you were in before the HUD
    }

    public string Focus(IntPtr hwnd) => ForegroundLock.Focus(hwnd);

    private static bool IsReal(IntPtr hwnd)
    {
        if (!Native.IsWindowVisible(hwnd) || Native.IsCloaked(hwnd))
            return false;
        var ex = (long)Native.GetWindowLongPtr(hwnd, Native.GwlExStyle);
        if ((ex & Native.WsExToolWindow) != 0)
            return false;
        var owner = Native.GetWindow(hwnd, Native.GwOwner);
        return owner == IntPtr.Zero || (ex & Native.WsExAppWindow) != 0;
    }

    private static (int Pid, string Name)? ProcessOf(IntPtr hwnd)
    {
        Native.GetWindowThreadProcessId(hwnd, out var pid);
        try
        {
            using var p = Process.GetProcessById((int)pid);
            return ((int)pid, p.ProcessName.ToLowerInvariant() + ".exe");
        }
        catch (ArgumentException)
        {
            return null;                      // exited meanwhile
        }
    }
}

/// <summary>
/// Bringing a window to the front from a background process. Windows' foreground
/// lock makes a bare SetForegroundWindow fail unless the caller received the
/// last input event — measured 1/31 for the old Python tool. Measured on this
/// machine (60/60 normal, 60/60 minimized): injecting a zero-distance mouse
/// move first (no visible effect) satisfies that rule. Each step is verified
/// with GetForegroundWindow; later steps are fallbacks, never stacked blindly.
/// </summary>
public static class ForegroundLock
{
    private static readonly TimeSpan VerifyFor = TimeSpan.FromMilliseconds(250);

    public static string Focus(IntPtr hwnd)
    {
        if (!Native.IsWindow(hwnd))
            throw new InvalidOperationException("the window no longer exists");
        if (Native.IsIconic(hwnd))
            Native.ShowWindow(hwnd, Native.SwRestore);
        if (Native.GetForegroundWindow() == hwnd)
            return "already-foreground";

        var tried = new List<string>();
        foreach (var (name, step) in Strategies)
        {
            tried.Add(name);
            step(hwnd);
            if (WaitForeground(hwnd))
                return name;
        }
        throw new InvalidOperationException($"Windows kept the window in the background (tried {string.Join(", ", tried)})");
    }

    private static readonly (string Name, Action<IntPtr> Step)[] Strategies =
    [
        ("input-unlock", h => { SendZeroMouseMove(); Native.SetForegroundWindow(h); }),
        ("attach-thread-input", AttachAndActivate),
        ("key-unlock", h => { SendInertKey(); Native.SetForegroundWindow(h); }),
    ];

    private static bool WaitForeground(IntPtr hwnd)
    {
        var sw = Stopwatch.StartNew();
        while (sw.Elapsed < VerifyFor)
        {
            if (Native.GetForegroundWindow() == hwnd)
                return true;
            Thread.Sleep(5);
        }
        return false;
    }

    /// <summary>A mouse "move" of 0,0: counts as input, moves nothing.</summary>
    private static void SendZeroMouseMove()
    {
        var move = new Native.Input
        {
            Type = Native.InputMouse,
            U = new Native.InputUnion { Mi = new Native.MouseInput { Flags = Native.MouseEventFMove } },
        };
        Native.SendInput(1, [move], Marshal.SizeOf<Native.Input>());
    }

    /// <summary>Down+up of an unassigned virtual key (0xE8): no app binds it.</summary>
    private static void SendInertKey()
    {
        var down = new Native.Input { Type = Native.InputKeyboard, U = new Native.InputUnion { Ki = new Native.KeybdInput { Vk = 0xE8 } } };
        var up = new Native.Input { Type = Native.InputKeyboard, U = new Native.InputUnion { Ki = new Native.KeybdInput { Vk = 0xE8, Flags = Native.KeyEventFKeyUp } } };
        Native.SendInput(2, [down, up], Marshal.SizeOf<Native.Input>());
    }

    private static void AttachAndActivate(IntPtr hwnd)
    {
        var me = Native.GetCurrentThreadId();
        var fg = Native.GetWindowThreadProcessId(Native.GetForegroundWindow(), out _);
        var target = Native.GetWindowThreadProcessId(hwnd, out _);
        var attachedFg = fg != 0 && fg != me && Native.AttachThreadInput(me, fg, true);
        var attachedTarget = target != me && target != fg && Native.AttachThreadInput(me, target, true);
        try
        {
            Native.BringWindowToTop(hwnd);
            Native.SetForegroundWindow(hwnd);
            Native.SetFocus(hwnd);
        }
        finally
        {
            if (attachedFg) Native.AttachThreadInput(me, fg, false);
            if (attachedTarget) Native.AttachThreadInput(me, target, false);
        }
    }
}
