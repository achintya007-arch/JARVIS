using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Bbp.Client.Tests;

public class EnvelopeTests
{
    [Fact]
    public void Create_SerializesExactlyTheSevenFields_WithExplicitNullCorr()
    {
        var json = Envelope.Create("ping", "ping").ToJson();
        var obj = JsonNode.Parse(json)!.AsObject();

        Assert.Equal(["v", "id", "kind", "topic", "ts", "corr", "payload"], obj.Select(p => p.Key));
        Assert.Equal(1, (int)obj["v"]!);
        Assert.Null(obj["corr"]);
        Assert.Contains("\"corr\":null", json);
        Assert.Empty(obj["payload"]!.AsObject());
    }

    [Fact]
    public void Timestamp_IsIso8601WithOffset_AsTheSchemaRequires()
    {
        var ts = Envelope.Create("ping", "ping").Ts;
        Assert.Matches(new Regex(@"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}(Z|[+-]\d{2}:\d{2})$"), ts);
    }

    [Fact]
    public void Ids_AreUnique()
    {
        var ids = Enumerable.Range(0, 200).Select(_ => Envelope.Create("ping", "ping").Id).ToHashSet();
        Assert.Equal(200, ids.Count);
    }

    [Fact]
    public void Payload_UsesSnakeCase()
    {
        var json = Envelope.Create("event", "reply.chunk", new ReplyChunkPayload("t1", 2, "Hi.")).ToJson();
        Assert.Contains("\"turn_id\":\"t1\"", json);
        Assert.Contains("\"seq\":2", json);
    }

    [Fact]
    public void RoundTrip_PreservesEverything()
    {
        var original = Envelope.Create("response", "error",
            new ErrorPayload(false, new ErrorInfo("E_BUSY", "busy")), corr: "abc");
        var back = Envelope.FromNode(JsonNode.Parse(original.ToJson())!);

        Assert.Equal(original.Id, back.Id);
        Assert.Equal(original.Ts, back.Ts);
        Assert.Equal("abc", back.Corr);
        Assert.Equal(new ErrorPayload(false, new ErrorInfo("E_BUSY", "busy")), back.PayloadAs<ErrorPayload>());
    }

    [Fact]
    public void ParsesABrainWelcome_ProducedByPython()
    {
        // Shape exactly as core_v3/adapters/bbp_server.py sends it.
        const string raw = """
            {"v":1,"id":"5f0c3e8a9b2d4c1e8f7a6b5c4d3e2f1a","kind":"welcome","topic":"welcome",
             "ts":"2026-10-15T09:30:01.123+05:30","corr":null,
             "payload":{"session":"abc","brain_version":"3.0","snapshot":{
               "voice":{"state":"listening","muted":false},
               "agent":{"mood":"focused","intent":"task","model":"qwen2.5:7b","load":{"ram_pct":71.5,"vram_pct":null}},
               "active_goals":[{"name":"drink water","priority":5}],
               "pending_confirmations":[{"confirm_id":"c1","prompt":"Run it?","risk":"high","timeout_s":15}]}}}
            """;
        var node = JsonNode.Parse(raw);
        Brain.Validator.Validate(node);
        var welcome = Envelope.FromNode(node!).PayloadAs<WelcomePayload>();

        Assert.Equal("3.0", welcome.BrainVersion);
        Assert.Equal("listening", welcome.Snapshot.Voice.State);
        Assert.Equal(71.5, welcome.Snapshot.Agent.Load.RamPct);
        Assert.Null(welcome.Snapshot.Agent.Load.VramPct);
        Assert.Equal(new GoalSnapshot("drink water", 5), welcome.Snapshot.ActiveGoals.Single());
        Assert.Equal(15, welcome.Snapshot.PendingConfirmations.Single().TimeoutS);
    }

    [Fact]
    public void UnknownPayloadFields_AreIgnored()
    {
        var node = JsonNode.Parse(Envelope.Create("event", "voice.state",
            new JsonObject { ["state"] = "idle", ["muted"] = true, ["future"] = 1 }).ToJson())!;
        Brain.Validator.Validate(node);
        Assert.Equal(new VoiceStatePayload("idle", true), Envelope.FromNode(node).PayloadAs<VoiceStatePayload>());
    }
}
