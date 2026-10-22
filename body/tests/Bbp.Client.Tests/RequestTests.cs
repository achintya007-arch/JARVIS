using System.Text.Json.Nodes;

namespace Bbp.Client.Tests;

/// <summary>Brain → Body requests (amendment 14): every request gets exactly one
/// response, correlated by corr, either { ok: true, result } on the request's
/// topic or an error response.</summary>
public class RequestTests
{
    /// <summary>Connect a client to a fake Brain, send it one raw request, return the reply.</summary>
    private static async Task<JsonObject?> RoundTrip(Action<BbpClient> register, string requestJson,
                                                     TimeSpan? budget = null, TimeSpan? wait = null)
    {
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        var replied = new TaskCompletionSource<JsonObject?>(TaskCreationOptions.RunContinuationsAsynchronously);
        var socket = new FakeSocket
        {
            OnSend = (msg, s) =>
            {
                var kind = (string)msg["kind"]!;
                if (kind == "hello")
                    s.Push(Brain.Welcome());
                else if (kind == "response")
                    replied.TrySetResult(msg);
            },
        };
        var opts = new BbpClientOptions
        {
            TokenProvider = () => "tok",
            PingInterval = TimeSpan.FromMilliseconds(4321),
            RequestBudget = budget ?? TimeSpan.FromSeconds(2),
        };
        var client = new BbpClient(opts, Brain.Validator, new FakeFactory(n => n == 1 ? socket : null),
            (d, ct) => d == opts.PingInterval ? Task.Delay(d, ct) : Task.Delay(Timeout.Infinite, ct));
        register(client);
        client.Welcomed += _ => socket.PushRaw(requestJson);
        var run = client.RunAsync(cts.Token);
        var winner = await Task.WhenAny(replied.Task, Task.Delay(wait ?? TimeSpan.FromSeconds(3)));
        await cts.CancelAsync();
        await run;
        return winner == replied.Task ? replied.Task.Result : null;
    }

    private static string Request(string topic, object payload, string id = "req-1") =>
        Envelope.Create("request", topic, payload) with { Id = id } is var e ? e.ToJson() : "";

    private static void AssertError(JsonObject? reply, string code, string corr = "req-1")
    {
        Assert.NotNull(reply);
        Brain.Validator.Validate(reply);
        Assert.Equal("response", (string)reply!["kind"]!);
        Assert.Equal("error", (string)reply["topic"]!);
        Assert.Equal(corr, (string)reply["corr"]!);
        Assert.Equal(code, (string)reply["payload"]!["error"]!["code"]!);
    }

    [Fact]
    public async Task Result_IsSentOnTheRequestTopic_WithCorr()
    {
        var reply = await RoundTrip(c => c.HandleRequests("desktop.window.list",
            (_, _) => Task.FromResult<object>(new { windows = new[] { new { title = "a", app = "notepad", hwnd = 7, allowed = true } } })),
            Request("desktop.window.list", new { }));

        Assert.NotNull(reply);
        Brain.Validator.Validate(reply);                         // response schema, amendment 14
        Assert.Equal("desktop.window.list", (string)reply!["topic"]!);
        Assert.Equal("req-1", (string)reply["corr"]!);
        Assert.True((bool)reply["payload"]!["ok"]!);
        Assert.Equal("notepad", (string)reply["payload"]!["result"]!["windows"]![0]!["app"]!);
    }

    [Fact]
    public async Task UnknownTopic_IsAnswered_E_UNKNOWN_TOPIC() =>
        AssertError(await RoundTrip(_ => { }, Request("desktop.window.list", new { })), "E_UNKNOWN_TOPIC");

    [Fact]
    public async Task InvalidPayload_IsAnswered_E_BAD_PAYLOAD()
    {
        var ran = false;
        var reply = await RoundTrip(c => c.HandleRequests("desktop.audio.set_volume",
            (_, _) => { ran = true; return Task.FromResult<object>(new { level = 1, muted = false }); }),
            Request("desktop.audio.set_volume", new { level = 150 }));
        AssertError(reply, "E_BAD_PAYLOAD");
        Assert.False(ran);
    }

    [Fact]
    public async Task HandlerRefusal_KeepsItsCode()
    {
        var reply = await RoundTrip(c => c.HandleRequests("desktop.app.launch",
            (_, _) => throw new BbpException("E_NOT_ALLOWED", "cmd isn't on the Body's allowlist")),
            Request("desktop.app.launch", new { app_id = "cmd" }));
        AssertError(reply, "E_NOT_ALLOWED");
        Assert.Contains("cmd", (string)reply!["payload"]!["error"]!["message"]!);
    }

    [Fact]
    public async Task HandlerCrash_Is_E_INTERNAL() =>
        AssertError(await RoundTrip(c => c.HandleRequests("desktop.window.list",
            (_, _) => throw new NullReferenceException("oops")),
            Request("desktop.window.list", new { })), "E_INTERNAL");

    [Fact]
    public async Task ResultThatBreaksTheSchema_Is_E_INTERNAL_NotSent() =>
        AssertError(await RoundTrip(c => c.HandleRequests("desktop.audio.set_volume",
            (_, _) => Task.FromResult<object>(new { level = 300 })),      // no "muted", level out of range
            Request("desktop.audio.set_volume", new { level = 30 })), "E_INTERNAL");

    [Fact]
    public async Task SlowHandler_Is_E_TIMEOUT_WithinTheBudget() =>
        AssertError(await RoundTrip(c => c.HandleRequests("desktop.window.list",
            async (_, ct) => { await Task.Delay(Timeout.Infinite, ct); return new { }; }),
            Request("desktop.window.list", new { }), budget: TimeSpan.FromMilliseconds(100)), "E_TIMEOUT");

    [Fact]
    public async Task RequestWithoutId_GetsNoResponse()
    {
        var node = JsonNode.Parse(Request("desktop.window.list", new { }))!.AsObject();
        node.Remove("id");
        var reply = await RoundTrip(_ => { }, node.ToJsonString(), wait: TimeSpan.FromMilliseconds(500));
        Assert.Null(reply);
    }
}
