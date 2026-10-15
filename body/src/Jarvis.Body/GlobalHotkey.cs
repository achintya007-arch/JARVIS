using System.ComponentModel;
using System.Runtime.InteropServices;

namespace Jarvis.Body;

/// <summary>A key combination such as "Ctrl+Alt+J", parsed for RegisterHotKey.</summary>
public readonly record struct HotkeyGesture(uint Modifiers, uint VirtualKey, string Display)
{
    public const uint ModAlt = 0x0001;
    public const uint ModControl = 0x0002;
    public const uint ModShift = 0x0004;
    public const uint ModWin = 0x0008;
    public const uint ModNoRepeat = 0x4000;     // holding the keys fires once

    /// <summary>Accepts Ctrl/Control, Alt, Shift, Win plus one key: A–Z, 0–9,
    /// F1–F24 or Space, joined with '+'. Needs Ctrl, Alt or Win so a global
    /// hotkey can't swallow ordinary typing.</summary>
    public static bool TryParse(string? text, out HotkeyGesture gesture, out string error)
    {
        gesture = default;
        error = "";
        if (string.IsNullOrWhiteSpace(text))
        {
            error = "hotkey is empty";
            return false;
        }

        uint mods = 0;
        uint? vk = null;
        var names = new List<string>();
        foreach (var raw in text.Split('+'))
        {
            var part = raw.Trim();
            uint mod = part.ToLowerInvariant() switch
            {
                "ctrl" or "control" => ModControl,
                "alt" => ModAlt,
                "shift" => ModShift,
                "win" or "windows" => ModWin,
                _ => 0,
            };
            if (mod != 0)
            {
                if ((mods & mod) != 0)
                {
                    error = $"'{part}' appears twice";
                    return false;
                }
                mods |= mod;
                names.Add(ModName(mod));
                continue;
            }
            if (vk is not null)
            {
                error = $"more than one key ('{part}')";
                return false;
            }
            vk = KeyCode(part);
            if (vk is null)
            {
                error = $"unknown key '{part}'";
                return false;
            }
            names.Add(part.Length == 1 ? part.ToUpperInvariant() : char.ToUpperInvariant(part[0]) + part[1..].ToLowerInvariant());
        }

        if (vk is null)
        {
            error = "no key, only modifiers";
            return false;
        }
        if ((mods & (ModControl | ModAlt | ModWin)) == 0)
        {
            error = "needs Ctrl, Alt or Win";
            return false;
        }
        gesture = new HotkeyGesture(mods | ModNoRepeat, vk.Value, string.Join("+", names));
        return true;
    }

    private static string ModName(uint mod) => mod switch
    {
        ModControl => "Ctrl",
        ModAlt => "Alt",
        ModShift => "Shift",
        _ => "Win",
    };

    private static uint? KeyCode(string key)
    {
        if (key.Length == 1 && char.IsAsciiLetter(key[0]))
            return char.ToUpperInvariant(key[0]);                  // VK_A..VK_Z = 'A'..'Z'
        if (key.Length == 1 && char.IsAsciiDigit(key[0]))
            return key[0];                                         // VK_0..VK_9 = '0'..'9'
        if (key.Equals("space", StringComparison.OrdinalIgnoreCase))
            return 0x20;
        if (key.Length is 2 or 3 && (key[0] is 'F' or 'f') && int.TryParse(key[1..], out var n) && n is >= 1 and <= 24)
            return (uint)(0x70 + n - 1);                           // VK_F1 = 0x70
        return null;
    }
}

/// <summary>
/// A system-wide hotkey via Win32 RegisterHotKey. Registration fails when
/// another app already owns the combination; the caller reports that and
/// carries on (no hotkey, everything else works).
/// </summary>
public sealed class GlobalHotkey(HotkeyGesture gesture, int id = 0x4A42) : IDisposable
{
    public const int WmHotkey = 0x0312;

    private IntPtr _hwnd;
    private bool _registered;

    public HotkeyGesture Gesture => gesture;

    public event Action? Pressed;

    /// <summary>Register for <paramref name="hwnd"/> (IntPtr.Zero = the calling
    /// thread). On failure <paramref name="error"/> holds the Win32 reason.</summary>
    public bool TryRegister(IntPtr hwnd, out string error)
    {
        error = "";
        if (_registered)
            return true;
        if (!NativeMethods.RegisterHotKey(hwnd, id, gesture.Modifiers, gesture.VirtualKey))
        {
            error = new Win32Exception(Marshal.GetLastWin32Error()).Message;
            return false;
        }
        _hwnd = hwnd;
        _registered = true;
        return true;
    }

    /// <summary>Feed window messages here; true if it was our hotkey.</summary>
    public bool HandleMessage(int msg, IntPtr wParam)
    {
        if (msg != WmHotkey || wParam.ToInt32() != id)
            return false;
        Pressed?.Invoke();
        return true;
    }

    public void Dispose()
    {
        if (_registered)
            NativeMethods.UnregisterHotKey(_hwnd, id);
        _registered = false;
    }

    private static class NativeMethods
    {
        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool RegisterHotKey(IntPtr hWnd, int id, uint fsModifiers, uint vk);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool UnregisterHotKey(IntPtr hWnd, int id);
    }
}
