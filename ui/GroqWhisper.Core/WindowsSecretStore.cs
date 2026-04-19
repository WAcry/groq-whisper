using System.Security.Cryptography;
using System.Text;

namespace GroqWhisper.Core;

public sealed class WindowsSecretStore
{
    private readonly string _secretPath;
    private readonly ISecretProtector _protector;

    public WindowsSecretStore(string? baseDirectory = null, ISecretProtector? protector = null)
    {
        var root = baseDirectory ?? Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "GroqWhisper",
            "secrets");
        _secretPath = Path.Combine(root, "groq-api-key.dat");
        _protector = protector ?? new DpapiSecretProtector();
    }

    public bool HasGroqApiKey() => File.Exists(_secretPath);

    public string? LoadGroqApiKey()
    {
        if (!File.Exists(_secretPath))
            return null;

        try
        {
            var protectedBytes = File.ReadAllBytes(_secretPath);
            var plaintextBytes = _protector.Unprotect(protectedBytes);
            try
            {
                return Encoding.UTF8.GetString(plaintextBytes);
            }
            finally
            {
                Array.Clear(plaintextBytes, 0, plaintextBytes.Length);
            }
        }
        catch (CryptographicException ex)
        {
            throw new InvalidOperationException(
                "Stored API key could not be decrypted. Clear it and enter the key again.",
                ex);
        }
    }

    public void SaveGroqApiKey(string apiKey)
    {
        if (string.IsNullOrWhiteSpace(apiKey))
            throw new ArgumentException("API key cannot be empty.", nameof(apiKey));

        var normalized = apiKey.Trim();
        var plaintextBytes = Encoding.UTF8.GetBytes(normalized);
        byte[]? protectedBytes = null;
        var directory = Path.GetDirectoryName(_secretPath)
            ?? throw new InvalidOperationException("Secret path has no parent directory.");
        Directory.CreateDirectory(directory);
        var tempPath = Path.Combine(directory, $"{Path.GetFileName(_secretPath)}.{Guid.NewGuid():N}.tmp");

        try
        {
            protectedBytes = _protector.Protect(plaintextBytes);
            File.WriteAllBytes(tempPath, protectedBytes);
            if (File.Exists(_secretPath))
            {
                File.Replace(tempPath, _secretPath, null, ignoreMetadataErrors: true);
            }
            else
            {
                File.Move(tempPath, _secretPath);
            }
        }
        finally
        {
            Array.Clear(plaintextBytes, 0, plaintextBytes.Length);
            if (protectedBytes is not null)
                Array.Clear(protectedBytes, 0, protectedBytes.Length);
            if (File.Exists(tempPath))
                File.Delete(tempPath);
        }
    }

    public void ClearGroqApiKey()
    {
        if (File.Exists(_secretPath))
            File.Delete(_secretPath);
    }
}
