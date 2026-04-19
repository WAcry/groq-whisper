using System.Security.Cryptography;
using System.Text;
using GroqWhisper.Core;
using Xunit;

namespace GroqWhisper.Tests;

public sealed class WindowsSecretStoreTests : IDisposable
{
    private readonly string _tempDirectory = Path.Combine(Path.GetTempPath(), $"gw-store-{Guid.NewGuid():N}");

    [Fact]
    public void SaveLoadNormalizeAndClearRoundTrip()
    {
        var store = new WindowsSecretStore(_tempDirectory, new PassthroughSecretProtector());

        store.SaveGroqApiKeys(["  test-key  ", "", "test-key", " second-key "]);

        Assert.True(store.HasGroqApiKeys());
        Assert.True(store.HasGroqApiKey());
        Assert.Equal(["test-key", "second-key"], store.LoadGroqApiKeys());
        Assert.Equal("test-key", store.LoadGroqApiKey());

        store.ClearGroqApiKeys();

        Assert.False(store.HasGroqApiKeys());
        Assert.Empty(store.LoadGroqApiKeys());
        Assert.Null(store.LoadGroqApiKey());
    }

    [Fact]
    public void SaveReplacesExistingValueAtomically()
    {
        var store = new WindowsSecretStore(_tempDirectory, new PassthroughSecretProtector());

        store.SaveGroqApiKeys(["old-key", "older-key"]);
        store.SaveGroqApiKeys(["new-key"]);

        Assert.Equal(["new-key"], store.LoadGroqApiKeys());
    }

    [Fact]
    public void SingularSaveUsesNewEnvelopeFormat()
    {
        var store = new WindowsSecretStore(_tempDirectory, new PassthroughSecretProtector());

        store.SaveGroqApiKey("  test-key  ");

        Assert.Equal(["test-key"], store.LoadGroqApiKeys());
        Assert.Equal("test-key", store.LoadGroqApiKey());
    }

    [Fact]
    public void SaveRejectsEmptyKeySetAfterNormalization()
    {
        var store = new WindowsSecretStore(_tempDirectory, new PassthroughSecretProtector());

        var ex = Assert.Throws<ArgumentException>(() => store.SaveGroqApiKeys([" ", "", "\t"]));

        Assert.Contains("At least one API key", ex.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void LoadRejectsLegacySingleStringPayload()
    {
        var store = new WindowsSecretStore(_tempDirectory, new PassthroughSecretProtector());
        Directory.CreateDirectory(_tempDirectory);
        File.WriteAllBytes(
            Path.Combine(_tempDirectory, "groq-api-key.dat"),
            Encoding.UTF8.GetBytes("legacy-key"));

        var ex = Assert.Throws<InvalidOperationException>(() => store.LoadGroqApiKeys());

        Assert.Contains("older unsupported format", ex.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void LoadAcceptsValidSingleKeyEnvelope()
    {
        var store = new WindowsSecretStore(_tempDirectory, new PassthroughSecretProtector());
        Directory.CreateDirectory(_tempDirectory);
        File.WriteAllBytes(
            Path.Combine(_tempDirectory, "groq-api-key.dat"),
            Encoding.UTF8.GetBytes("""
                {"Version":1,"ApiKeys":["single-key"]}
                """));

        Assert.Equal(["single-key"], store.LoadGroqApiKeys());
    }

    [Fact]
    public void LoadRejectsMalformedEnvelopeWithNullEntry()
    {
        var store = new WindowsSecretStore(_tempDirectory, new PassthroughSecretProtector());
        Directory.CreateDirectory(_tempDirectory);
        File.WriteAllBytes(
            Path.Combine(_tempDirectory, "groq-api-key.dat"),
            Encoding.UTF8.GetBytes("""
                {"Version":1,"ApiKeys":[null,"second-key"]}
                """));

        var ex = Assert.Throws<InvalidOperationException>(() => store.LoadGroqApiKeys());

        Assert.Contains("unsupported format", ex.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void LoadWrapsCryptographicErrors()
    {
        var store = new WindowsSecretStore(_tempDirectory, new ThrowingUnprotectSecretProtector());
        Directory.CreateDirectory(_tempDirectory);
        File.WriteAllBytes(Path.Combine(_tempDirectory, "groq-api-key.dat"), [1, 2, 3]);

        var ex = Assert.Throws<InvalidOperationException>(() => store.LoadGroqApiKeys());

        Assert.Contains("could not be decrypted", ex.Message, StringComparison.OrdinalIgnoreCase);
    }

    public void Dispose()
    {
        if (Directory.Exists(_tempDirectory))
            Directory.Delete(_tempDirectory, recursive: true);
    }

    private sealed class PassthroughSecretProtector : ISecretProtector
    {
        public byte[] Protect(byte[] plaintext) => [.. plaintext];
        public byte[] Unprotect(byte[] protectedBytes) => [.. protectedBytes];
    }

    private sealed class ThrowingUnprotectSecretProtector : ISecretProtector
    {
        public byte[] Protect(byte[] plaintext) => [.. plaintext];

        public byte[] Unprotect(byte[] protectedBytes)
        {
            throw new CryptographicException("corrupt");
        }
    }
}
