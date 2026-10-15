using System.Text.Json.Nodes;

namespace Bbp.Client.Tests;

public class SchemaValidatorTests
{
    private static SchemaValidator V => Brain.Validator;

    private static string CodeOf(JsonNode node) =>
        Assert.Throws<BbpException>(() => V.Validate(node)).Code;

    private static JsonObject Node(Envelope e) => e.ToNode().AsObject();

    [Fact]
    public void SchemasShipNextToTheBinaries()
    {
        Assert.True(File.Exists(Path.Combine(V.Directory, "envelope.schema.json")));
        foreach (var topic in new[] { "voice.state", "voice.transcript", "reply.chunk", "reply.done", "error" })
            Assert.True(V.HasSchema("event", topic), topic);
        foreach (var kind in new[] { "hello", "welcome", "ping", "pong" })
            Assert.True(V.HasSchema(kind, kind), kind);
    }

    [Fact]
    public void EveryMessageTheBodySends_Validates()
    {
        V.Validate(Envelope.Create("hello", "hello", new HelloPayload("win-shell", "0.1.0", "tok", ["hud"])));
        V.Validate(Envelope.Create("ping", "ping"));
    }

    [Fact]
    public void BrainEvents_Validate()
    {
        V.Validate(Brain.Welcome());
        V.Validate(Brain.Event("voice.state", new VoiceStatePayload("speaking", false)));
        V.Validate(Brain.Event("voice.transcript", new TranscriptPayload("open chrome", true, null)));
        V.Validate(Brain.Event("reply.chunk", new ReplyChunkPayload("t", 0, "Hi.")));
        V.Validate(Brain.Event("reply.done", new ReplyDonePayload("t", true)));
        V.Validate(Envelope.Create("pong", "pong", corr: "x"));
    }

    [Fact]
    public void WrongVersion_Rejected()
    {
        var n = Node(Envelope.Create("ping", "ping"));
        n["v"] = 2;
        Assert.Equal("E_BAD_PAYLOAD", CodeOf(n));
    }

    [Fact]
    public void MissingField_Rejected()
    {
        var n = Node(Envelope.Create("ping", "ping"));
        n.Remove("ts");
        Assert.Equal("E_BAD_PAYLOAD", CodeOf(n));
    }

    [Fact]
    public void SessionTopicMustEqualKind() =>
        Assert.Equal("E_BAD_PAYLOAD", CodeOf(Node(Envelope.Create("ping", "pong"))));

    [Fact]
    public void ResponseRequiresCorr() =>
        Assert.Equal("E_BAD_PAYLOAD", CodeOf(Node(Envelope.Create("response", "error",
            new ErrorPayload(false, new ErrorInfo("E_BUSY", "busy"))))));

    [Fact]
    public void BadVoiceState_Rejected() =>
        Assert.Equal("E_BAD_PAYLOAD", CodeOf(Node(Brain.Event("voice.state", new VoiceStatePayload("dancing", false)))));

    [Fact]
    public void ConfidenceOutOfRange_Rejected() =>
        Assert.Equal("E_BAD_PAYLOAD", CodeOf(Node(Brain.Event("voice.transcript", new TranscriptPayload("x", true, 1.5)))));

    [Fact]
    public void HelloWithEmptyToken_Rejected() =>
        Assert.Equal("E_BAD_PAYLOAD", CodeOf(Node(Envelope.Create("hello", "hello",
            new HelloPayload("win-shell", "0.1.0", "", [])))));

    [Fact]
    public void UnknownTopic_IsUnknown() =>
        Assert.Equal("E_UNKNOWN_TOPIC", CodeOf(Node(Brain.Event("sentinel.finding", new { }))));
}
