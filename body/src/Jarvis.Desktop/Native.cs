using System.Runtime.InteropServices;
using System.Text;

namespace Jarvis.Desktop;

/// <summary>Win32 calls used by the desktop actions.</summary>
internal static class Native
{
    public const int SwRestore = 9;
    public const int GwlExStyle = -20;
    public const long WsExToolWindow = 0x00000080;
    public const long WsExAppWindow = 0x00040000;
    public const uint GwOwner = 4;
    public const int DwmwaCloaked = 14;
    public const uint InputMouse = 0;
    public const uint InputKeyboard = 1;
    public const uint MouseEventFMove = 0x0001;
    public const uint KeyEventFKeyUp = 0x0002;

    public delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr lParam);

    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc proc, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowTextLengthW(IntPtr hwnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr hwnd, StringBuilder text, int max);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern IntPtr SetFocus(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hwnd, int cmd);
    [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr hwnd, uint cmd);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")] public static extern IntPtr GetWindowLongPtr(IntPtr hwnd, int index);
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool attach);
    [DllImport("user32.dll", SetLastError = true)] public static extern uint SendInput(uint count, Input[] inputs, int size);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr hwnd, int attr, out int value, int size);

    [StructLayout(LayoutKind.Sequential)]
    public struct MouseInput { public int Dx, Dy; public uint MouseData, Flags, Time; public IntPtr ExtraInfo; }

    [StructLayout(LayoutKind.Sequential)]
    public struct KeybdInput { public ushort Vk, Scan; public uint Flags, Time; public IntPtr ExtraInfo; }

    [StructLayout(LayoutKind.Explicit)]
    public struct InputUnion { [FieldOffset(0)] public MouseInput Mi; [FieldOffset(0)] public KeybdInput Ki; }

    /// <summary>Win32 INPUT; the union must be sized for MOUSEINPUT or SendInput rejects it.</summary>
    [StructLayout(LayoutKind.Sequential)]
    public struct Input { public uint Type; public InputUnion U; }

    public static string WindowText(IntPtr hwnd)
    {
        var n = GetWindowTextLengthW(hwnd);
        if (n <= 0)
            return "";
        var sb = new StringBuilder(n + 1);
        GetWindowTextW(hwnd, sb, sb.Capacity);
        return sb.ToString();
    }

    public static bool IsCloaked(IntPtr hwnd) =>
        DwmGetWindowAttribute(hwnd, DwmwaCloaked, out var v, sizeof(int)) == 0 && v != 0;
}
