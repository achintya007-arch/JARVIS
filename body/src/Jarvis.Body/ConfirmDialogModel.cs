using System.ComponentModel;
using Bbp.Client;

namespace Jarvis.Body;

/// <summary>
/// What a confirmation dialog shows and allows (BBP milestone 3). No WPF types,
/// so the rules are unit-tested: Deny is the default; a high-risk action's
/// Approve can't be reached from the keyboard; after the deadline nothing can
/// be sent; one answer per dialog.
/// </summary>
public sealed class ConfirmDialogModel(ConfirmationDialog dialog, Func<DateTimeOffset> clock) : INotifyPropertyChanged
{
    private bool _sending;
    private string _status = "";

    public event PropertyChangedEventHandler? PropertyChanged;

    public ConfirmationDialog Dialog { get; } = dialog;
    public string ConfirmId => Dialog.ConfirmId;
    public string Prompt => Dialog.Prompt;

    /// <summary>Exactly what would run. May come from the LLM: always shown as plain text.</summary>
    public string Detail => Dialog.Detail;
    public bool HasDetail => Detail.Length > 0;
    public bool HighRisk => Dialog.HighRisk;
    public string RiskLabel => HighRisk ? "High risk · click Approve to allow" : "Low risk";

    /// <summary>Keyboard focus (Tab, then Enter or Space) can reach Approve only for low-risk actions.</summary>
    public bool ApproveFocusable => !HighRisk;

    public double FractionLeft => Dialog.FractionLeft(clock());
    public string TimeLeft => $"{Math.Ceiling(Dialog.Remaining(clock()).TotalSeconds):0} s";
    public bool Expired => Dialog.Remaining(clock()) <= TimeSpan.Zero;
    public bool CanAnswer => !_sending && !Expired;

    /// <summary>"Sending…", a send error, or "Time's up" under the buttons.</summary>
    public string Status => Expired && !_sending ? "Time's up: JARVIS won't do it." : _status;

    /// <summary>The click handlers call this; null means "don't send" (already
    /// answered, or too late).</summary>
    public ConfirmResponsePayload? Answer(bool approved)
    {
        if (!CanAnswer)
            return null;
        _sending = true;
        _status = approved ? "Approving…" : "Denying…";
        RaiseAll();
        return new ConfirmResponsePayload(ConfirmId, approved);
    }

    /// <summary>The answer couldn't be sent: you can try again while time is left.</summary>
    public void AnswerFailed(string message)
    {
        _sending = false;
        _status = message;
        RaiseAll();
    }

    /// <summary>Called by a timer to move the countdown.</summary>
    public void Tick() => RaiseAll();

    private void RaiseAll() =>
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(string.Empty));   // "" = every property
}
