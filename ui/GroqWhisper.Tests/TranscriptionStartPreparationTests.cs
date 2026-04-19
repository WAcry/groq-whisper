using GroqWhisper.Core;
using Xunit;

namespace GroqWhisper.Tests;

public sealed class TranscriptionStartPreparationTests : IDisposable
{
    private readonly string _tempDirectory = Path.Combine(Path.GetTempPath(), $"gw-start-{Guid.NewGuid():N}");

    [Fact]
    public void CreateLoadsAllStoredApiKeysIntoRequest()
    {
        var store = new WindowsSecretStore(_tempDirectory, new PassthroughSecretProtector());
        store.SaveGroqApiKeys(["first-key", "second-key"]);

        var result = TranscriptionStartPreparation.Create(
            store,
            model: "whisper-large-v3-turbo");

        Assert.Null(result.ErrorMessage);
        Assert.NotNull(result.Request);
        Assert.Equal("whisper-large-v3-turbo", result.Request!.Model);
        Assert.Equal(["first-key", "second-key"], result.Request.ApiKeys);
    }

    [Fact]
    public void CreateReturnsErrorWhenNoStoredApiKeysExist()
    {
        var store = new WindowsSecretStore(_tempDirectory, new PassthroughSecretProtector());

        var result = TranscriptionStartPreparation.Create(store);

        Assert.Null(result.Request);
        Assert.Contains("No Groq API keys", result.ErrorMessage, StringComparison.OrdinalIgnoreCase);
    }

    private sealed class PassthroughSecretProtector : ISecretProtector
    {
        public byte[] Protect(byte[] plaintext) => [.. plaintext];
        public byte[] Unprotect(byte[] protectedBytes) => [.. protectedBytes];
    }

    public void Dispose()
    {
        if (Directory.Exists(_tempDirectory))
            Directory.Delete(_tempDirectory, recursive: true);
    }
}
