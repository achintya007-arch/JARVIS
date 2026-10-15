using Bbp.Client;

namespace Jarvis.Body.Tests;

public class HotkeyGestureTests
{
    private const uint Ctrl = HotkeyGesture.ModControl, Alt = HotkeyGesture.ModAlt,
        Shift = HotkeyGesture.ModShift, Win = HotkeyGesture.ModWin, NoRepeat = HotkeyGesture.ModNoRepeat;

    [Theory]
    [InlineData("Ctrl+Alt+J", Ctrl | Alt, 0x4A, "Ctrl+Alt+J")]
    [InlineData("ctrl + alt + j", Ctrl | Alt, 0x4A, "Ctrl+Alt+J")]
    [InlineData("Control+Shift+Space", Ctrl | Shift, 0x20, "Ctrl+Shift+Space")]
    [InlineData("Win+F9", Win, 0x78, "Win+F9")]
    [InlineData("Alt+7", Alt, 0x37, "Alt+7")]
    [InlineData("Ctrl+F24", Ctrl, 0x87, "Ctrl+F24")]
    public void Parses(string text, uint mods, uint vk, string display)
    {
        Assert.True(HotkeyGesture.TryParse(text, out var g, out var error), error);
        Assert.Equal(mods | NoRepeat, g.Modifiers);       // holding the keys fires once
        Assert.Equal(vk, g.VirtualKey);
        Assert.Equal(display, g.Display);
    }

    [Theory]
    [InlineData("", "empty")]
    [InlineData("J", "needs Ctrl, Alt or Win")]
    [InlineData("Shift+J", "needs Ctrl, Alt or Win")]
    [InlineData("Ctrl+Alt", "no key")]
    [InlineData("Ctrl+Ctrl+J", "twice")]
    [InlineData("Ctrl+J+K", "more than one key")]
    [InlineData("Ctrl+Banana", "unknown key")]
    [InlineData("Ctrl+F25", "unknown key")]
    public void Rejects(string text, string reason)
    {
        Assert.False(HotkeyGesture.TryParse(text, out _, out var error));
        Assert.Contains(reason, error);
    }
}

public class GlobalHotkeyTests
{
    // An obscure combination no real app should own.
    private static HotkeyGesture Rare()
    {
        HotkeyGesture.TryParse("Ctrl+Alt+Shift+Win+F23", out var g, out _);
        return g;
    }

    [Fact]
    public void SecondRegistrationOfTheSameCombo_FailsWithAReason_WithoutThrowing()
    {
        using var first = new GlobalHotkey(Rare(), id: 0x7001);
        using var second = new GlobalHotkey(Rare(), id: 0x7002);

        if (!first.TryRegister(IntPtr.Zero, out _))
            return;     // no interactive desktop (some CI hosts): nothing to conflict with

        Assert.False(second.TryRegister(IntPtr.Zero, out var error));   // "already registered"
        Assert.False(string.IsNullOrWhiteSpace(error));
    }

    [Fact]
    public void HandleMessage_RaisesPressed_OnlyForItsOwnId()
    {
        using var hotkey = new GlobalHotkey(Rare(), id: 0x7003);
        var presses = 0;
        hotkey.Pressed += () => presses++;

        Assert.False(hotkey.HandleMessage(0x0100, 0x7003));                  // not WM_HOTKEY
        Assert.False(hotkey.HandleMessage(GlobalHotkey.WmHotkey, 0x7999));   // someone else's id
        Assert.True(hotkey.HandleMessage(GlobalHotkey.WmHotkey, 0x7003));
        Assert.Equal(1, presses);
    }
}

public class TrayVisualTests
{
    [Theory]
    [InlineData(ConnectionState.Connected, false, TrayStatus.Connected)]
    [InlineData(ConnectionState.Connected, true, TrayStatus.Muted)]
    [InlineData(ConnectionState.Connecting, false, TrayStatus.Offline)]
    [InlineData(ConnectionState.Disconnected, true, TrayStatus.Offline)]      // stale mute ignored offline
    [InlineData(ConnectionState.AuthFailed, false, TrayStatus.AuthFailed)]
    public void MapsConnectionAndMuteToOneGlanceableState(ConnectionState state, bool muted, TrayStatus expected) =>
        Assert.Equal(expected, TrayVisual.For(state, muted));

    [Fact]
    public void AuthFailedTooltip_SaysCheckToken() =>
        Assert.Contains("auth failed, check token", TrayVisual.Tooltip(TrayStatus.AuthFailed));

    [Fact]
    public void TooltipsFitTheNotifyIconLimit()
    {
        foreach (var s in Enum.GetValues<TrayStatus>())
            Assert.True(TrayVisual.Tooltip(s).Length < 128, s.ToString());
    }
}
