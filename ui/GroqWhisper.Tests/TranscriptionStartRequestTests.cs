using System.Text.Json;
using GroqWhisper.Core;
using Xunit;

namespace GroqWhisper.Tests;

public sealed class TranscriptionStartRequestTests
{
    [Fact]
    public void CreateSerializesApiKeysAsArray()
    {
        var request = TranscriptionStartRequest.Create(
            model: "whisper-large-v3-turbo",
            language: "en",
            prompt: "spell Roblox correctly",
            apiKeys: ["  first-key  ", "", "first-key", "second-key"]);

        var json = JsonSerializer.Serialize(request);
        using var document = JsonDocument.Parse(json);
        var root = document.RootElement;

        Assert.Equal("whisper-large-v3-turbo", root.GetProperty("model").GetString());
        Assert.Equal("en", root.GetProperty("language").GetString());
        Assert.Equal("spell Roblox correctly", root.GetProperty("prompt").GetString());
        var apiKeys = root.GetProperty("api_keys")
            .EnumerateArray()
            .Select(static item => item.GetString())
            .Cast<string>()
            .ToArray();
        Assert.Equal(["first-key", "second-key"], apiKeys);
        Assert.False(root.TryGetProperty("api_key", out _));
    }

    [Fact]
    public void CreateOmitsNullOrBlankValues()
    {
        var request = TranscriptionStartRequest.Create(
            model: "  ",
            language: null,
            prompt: "",
            apiKeys: [" ", "\t"]);

        var json = JsonSerializer.Serialize(request);
        using var document = JsonDocument.Parse(json);

        Assert.Empty(document.RootElement.EnumerateObject());
    }
}
