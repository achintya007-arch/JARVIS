using System.Net.WebSockets;
using System.Text;

namespace Bbp.Client;

/// <summary>A text-frame socket. Abstracted so the client's handshake,
/// heartbeat and reconnect logic can be tested without a network.</summary>
public interface IBbpSocket : IAsyncDisposable
{
    Task SendAsync(string text, CancellationToken ct);

    /// <summary>Next text message, or null once the socket has closed.</summary>
    Task<string?> ReceiveAsync(CancellationToken ct);

    /// <summary>Close code the peer sent (e.g. 4401), once closed.</summary>
    int? CloseStatus { get; }

    string? CloseDescription { get; }

    Task CloseAsync(int code, string reason, CancellationToken ct);
}

public interface IBbpSocketFactory
{
    Task<IBbpSocket> ConnectAsync(Uri uri, CancellationToken ct);
}

/// <summary>Real transport: System.Net.WebSockets.ClientWebSocket.</summary>
public sealed class WebSocketFactory : IBbpSocketFactory
{
    public async Task<IBbpSocket> ConnectAsync(Uri uri, CancellationToken ct)
    {
        var ws = new ClientWebSocket();
        ws.Options.KeepAliveInterval = TimeSpan.Zero;    // the protocol's ping/pong is the heartbeat
        try
        {
            await ws.ConnectAsync(uri, ct).ConfigureAwait(false);
        }
        catch
        {
            ws.Dispose();
            throw;
        }
        return new WebSocketTransport(ws);
    }
}

internal sealed class WebSocketTransport(ClientWebSocket ws) : IBbpSocket
{
    private const int MaxMessageBytes = 4 * 1024 * 1024;

    public int? CloseStatus => ws.CloseStatus is { } s ? (int)s : null;

    public string? CloseDescription => ws.CloseStatusDescription;

    public Task SendAsync(string text, CancellationToken ct) =>
        ws.SendAsync(Encoding.UTF8.GetBytes(text), WebSocketMessageType.Text, true, ct);

    public async Task<string?> ReceiveAsync(CancellationToken ct)
    {
        var buffer = new byte[16 * 1024];
        using var message = new MemoryStream();
        while (true)
        {
            WebSocketReceiveResult result;
            try
            {
                result = await ws.ReceiveAsync(buffer, ct).ConfigureAwait(false);
            }
            catch (WebSocketException)
            {
                return null;    // connection dropped without a close frame
            }
            if (result.MessageType == WebSocketMessageType.Close)
            {
                if (ws.State == WebSocketState.CloseReceived)
                {
                    try { await ws.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, "", CancellationToken.None).ConfigureAwait(false); }
                    catch (WebSocketException) { }
                }
                return null;
            }
            message.Write(buffer, 0, result.Count);
            if (message.Length > MaxMessageBytes)
                throw new BbpException("E_BAD_PAYLOAD", "message too large");
            if (!result.EndOfMessage)
                continue;
            if (result.MessageType == WebSocketMessageType.Binary)
            {
                message.SetLength(0);       // binary frames are reserved in v0.1: ignore
                continue;
            }
            return Encoding.UTF8.GetString(message.GetBuffer(), 0, (int)message.Length);
        }
    }

    public async Task CloseAsync(int code, string reason, CancellationToken ct)
    {
        if (ws.State is WebSocketState.Open or WebSocketState.CloseReceived)
        {
            try { await ws.CloseAsync((WebSocketCloseStatus)code, reason, ct).ConfigureAwait(false); }
            catch (Exception e) when (e is WebSocketException or OperationCanceledException) { ws.Abort(); }
        }
    }

    public ValueTask DisposeAsync()
    {
        ws.Dispose();
        return ValueTask.CompletedTask;
    }
}
