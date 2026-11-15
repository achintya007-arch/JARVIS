using Bbp.Client;

namespace Jarvis.Body.Tests;

/// <summary>The dialog's rules (milestone 3): Deny is the default, a high-risk
/// Approve can't be reached from the keyboard, one answer, nothing after the deadline.</summary>
public class ConfirmDialogTests
{
    private static readonly DateTimeOffset T0 = new(2026, 11, 16, 9, 0, 0, TimeSpan.Zero);

    private static (ConfirmDialogModel, Action<double>) Make(string risk = "high", double timeout = 15)
    {
        var now = T0;
        var dialog = new ConfirmationDialog("c1", "About to close Spotify. Shall I proceed, Sir?", "Spotify",
                                            risk, timeout, T0.AddSeconds(timeout));
        return (new ConfirmDialogModel(dialog, () => now), seconds => now = T0.AddSeconds(seconds));
    }

    [Fact]
    public void HighRisk_ApproveIsNotKeyboardReachable()
    {
        var (m, _) = Make("high");
        Assert.False(m.ApproveFocusable);
        Assert.Contains("click", m.RiskLabel);
    }

    [Fact]
    public void LowRisk_ApproveIsKeyboardReachable()
    {
        var (m, _) = Make("low");
        Assert.True(m.ApproveFocusable);
        Assert.Equal("Low risk", m.RiskLabel);
    }

    [Fact]
    public void OneAnswerPerDialog()
    {
        var (m, _) = Make();
        var first = m.Answer(true);
        Assert.Equal(new ConfirmResponsePayload("c1", true), first);
        Assert.Null(m.Answer(false));
        Assert.False(m.CanAnswer);
        Assert.Equal("Approving…", m.Status);
    }

    [Fact]
    public void NothingIsSentAfterTheDeadline()
    {
        var (m, at) = Make(timeout: 10);
        at(10.01);
        Assert.True(m.Expired);
        Assert.Null(m.Answer(true));
        Assert.StartsWith("Time's up", m.Status);
    }

    [Fact]
    public void AFailedSend_CanBeRetriedWhileTimeIsLeft()
    {
        var (m, _) = Make();
        m.Answer(false);
        m.AnswerFailed("Not connected to JARVIS.");
        Assert.True(m.CanAnswer);
        Assert.Equal("Not connected to JARVIS.", m.Status);
        Assert.NotNull(m.Answer(false));
    }

    [Fact]
    public void Countdown_RoundsUp()
    {
        var (m, at) = Make(timeout: 15);
        Assert.Equal("15 s", m.TimeLeft);
        at(3.2);
        Assert.Equal("12 s", m.TimeLeft);
        Assert.Equal(11.8 / 15, m.FractionLeft, 3);
    }

    [Fact]
    public void DetailIsShownAsIs()
    {
        var (m, _) = Make();
        Assert.True(m.HasDetail);
        Assert.Equal("Spotify", m.Detail);
    }

    [Theory]
    [InlineData("alert", 30)]
    [InlineData("warn", 15)]
    [InlineData("info", 10)]
    public void NotificationCard_StaysLongerForAlerts(string level, int seconds) =>
        Assert.Equal(TimeSpan.FromSeconds(seconds), NotificationView.ShowFor(level));

    [Fact]
    public void TrayNotification_NeedsText() =>
        Assert.Equal("Timer", NotificationView.TrayText("Timer", " "));
}

/// <summary>set_element_text (amendment 20): the dialog shows the whole text that would be typed.</summary>
public class ConfirmDialogTextTests
{
    [Fact]
    public void TypingDialogs_ShowTheExactText()
    {
        var now = new DateTimeOffset(2026, 11, 16, 9, 0, 0, TimeSpan.Zero);
        var typing = new ConfirmationDialog("c1", "About to type into 'Search' in Notepad. Shall I proceed, Sir?",
                                            "Notepad › Edit 'Search' ← 11 characters", "low", 15, now.AddSeconds(15),
                                            "hello\nworld");
        var m = new ConfirmDialogModel(typing, () => now);
        Assert.True(m.HasText);
        Assert.Equal("hello\nworld", m.Text);
        Assert.False(new ConfirmDialogModel(typing with { Text = null }, () => now).HasText);
    }
}
