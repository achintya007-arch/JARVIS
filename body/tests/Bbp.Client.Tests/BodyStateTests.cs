namespace Bbp.Client.Tests;

public class BodyStateTests
{
    private static Envelope Ev(string topic, object payload) => Brain.Event(topic, payload);

    [Fact]
    public void Welcome_SetsVoiceStateAndModel()
    {
        var s = new BodyState();
        s.ApplyWelcome(new WelcomePayload("s", "3.0", Brain.EmptySnapshot with
        {
            Voice = new VoiceStatePayload("listening", true),
        }));
        Assert.Equal("listening", s.VoiceState);
        Assert.True(s.Muted);
        Assert.Equal("test-model", s.Model);
    }

    [Fact]
    public void ReplyChunks_StreamInSeqOrder_EvenIfOutOfOrder()
    {
        var s = new BodyState();
        s.Apply(Ev("reply.chunk", new ReplyChunkPayload("t1", 1, "world.")));
        s.Apply(Ev("reply.chunk", new ReplyChunkPayload("t1", 0, "Hello")));
        Assert.Equal("Hello world.", s.Reply);
        Assert.False(s.ReplyDone);

        s.Apply(Ev("reply.done", new ReplyDonePayload("t1", false)));
        Assert.True(s.ReplyDone);
        Assert.False(s.ReplyInterrupted);
    }

    [Fact]
    public void NewTurn_ReplacesThePreviousReply()
    {
        var s = new BodyState();
        s.Apply(Ev("reply.chunk", new ReplyChunkPayload("t1", 0, "Old.")));
        s.Apply(Ev("reply.done", new ReplyDonePayload("t1", false)));
        s.Apply(Ev("reply.chunk", new ReplyChunkPayload("t2", 0, "New.")));
        Assert.Equal("New.", s.Reply);
        Assert.Equal("t2", s.TurnId);
        Assert.False(s.ReplyDone);
    }

    [Fact]
    public void Transcript_StartsANewExchange()
    {
        var s = new BodyState();
        s.Apply(Ev("reply.chunk", new ReplyChunkPayload("t1", 0, "Old.")));
        s.Apply(Ev("voice.transcript", new TranscriptPayload("what time is it", true, 0.9)));
        Assert.Equal("what time is it", s.Transcript);
        Assert.Equal(0.9, s.TranscriptConfidence);
        Assert.Equal("", s.Reply);
    }

    [Fact]
    public void InterruptedReply_IsFlagged()
    {
        var s = new BodyState();
        s.Apply(Ev("reply.chunk", new ReplyChunkPayload("t1", 0, "Well—")));
        s.Apply(Ev("reply.done", new ReplyDonePayload("t1", true)));
        Assert.True(s.ReplyInterrupted);
        Assert.Equal("Well—", s.Reply);
    }

    [Fact]
    public void VoiceState_Updates_UnknownTopicsIgnored()
    {
        var s = new BodyState();
        Assert.True(s.Apply(Ev("voice.state", new VoiceStatePayload("speaking", false))));
        Assert.Equal("speaking", s.VoiceState);
        Assert.False(s.Apply(Ev("agent.state", new { mood = "x" })));
    }
}

public class TokenLocatorTests
{
    [Fact]
    public void FindsJarvisDataTokenInAParentDirectory()
    {
        var root = Directory.CreateTempSubdirectory("bbp");
        try
        {
            var data = Directory.CreateDirectory(Path.Combine(root.FullName, "jarvis_v1", "data"));
            File.WriteAllText(Path.Combine(data.FullName, "bbp_token"), "  tok123 \n");
            var deep = Directory.CreateDirectory(Path.Combine(root.FullName, "body", "src", "bin"));

            var path = TokenLocator.FindPath(null, deep.FullName);

            Assert.Equal(Path.Combine(data.FullName, "bbp_token"), path);
            Assert.Equal("tok123", TokenLocator.ReadToken(path));
        }
        finally
        {
            root.Delete(recursive: true);
        }
    }

    [Fact]
    public void ConfiguredPath_WinsAndMissingFileReadsAsNull()
    {
        var root = Directory.CreateTempSubdirectory("bbp");
        try
        {
            var file = Path.Combine(root.FullName, "my_token");
            File.WriteAllText(file, "abc");
            Assert.Equal(file, TokenLocator.FindPath("my_token", root.FullName));
            Assert.Null(TokenLocator.ReadToken(Path.Combine(root.FullName, "missing")));
        }
        finally
        {
            root.Delete(recursive: true);
        }
    }
}
