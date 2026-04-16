using System.Security.Cryptography;
using GroqWhisper.Core;
using Xunit;

namespace GroqWhisper.Tests;

public sealed class WindowsSecretStoreTests : IDisposable
{
    private readonly string _tempDirectory = Path.Combine(Path.GetTempPath(), $"gw-store-{Guid.NewGuid():N}");

    [Fact]
    public void SaveLoadAndClearRoundTrip()
    {
        var store = new WindowsSecretStore(_tempDirectory, new PassthroughSecretProtector());

        store.SaveGroqApiKey("  test-key  ");

        Assert.True(store.HasGroqApiKey());
        Assert.Equal("test-key", store.LoadGroqApiKey());

        store.ClearGroqApiKey();

        Assert.False(store.HasGroqApiKey());
        Assert.Null(store.LoadGroqApiKey());
    }

    [Fact]
    public void SaveReplacesExistingValueAtomically()
    {
        var store = new WindowsSecretStore(_tempDirectory, new PassthroughSecretProtector());

        store.SaveGroqApiKey("old-key");
        store.SaveGroqApiKey("new-key");

        Assert.Equal("new-key", store.LoadGroqApiKey());
    }

    [Fact]
    public void LoadWrapsCryptographicErrors()
    {
        var store = new WindowsSecretStore(_tempDirectory, new ThrowingUnprotectSecretProtector());
        Directory.CreateDirectory(_tempDirectory);
        File.WriteAllBytes(Path.Combine(_tempDirectory, "groq-api-key.dat"), [1, 2, 3]);

        var ex = Assert.Throws<InvalidOperationException>(() => store.LoadGroqApiKey());

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
