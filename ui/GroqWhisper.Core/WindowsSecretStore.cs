using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace GroqWhisper.Core;

public sealed class WindowsSecretStore
{
    private const int SecretEnvelopeVersion = 1;
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

    public bool HasGroqApiKeys() => File.Exists(_secretPath);

    public bool HasGroqApiKey() => HasGroqApiKeys();

    public IReadOnlyList<string> LoadGroqApiKeys()
    {
        if (!File.Exists(_secretPath))
            return [];

        try
        {
            var protectedBytes = File.ReadAllBytes(_secretPath);
            var plaintextBytes = _protector.Unprotect(protectedBytes);
            try
            {
                return DeserializeEnvelope(Encoding.UTF8.GetString(plaintextBytes));
            }
            finally
            {
                Array.Clear(plaintextBytes, 0, plaintextBytes.Length);
            }
        }
        catch (CryptographicException ex)
        {
            throw new InvalidOperationException(
                "Stored API keys could not be decrypted. Clear them and enter the keys again.",
                ex);
        }
    }

    public string? LoadGroqApiKey()
    {
        return LoadGroqApiKeys().FirstOrDefault();
    }

    public void SaveGroqApiKeys(IEnumerable<string> apiKeys)
    {
        ArgumentNullException.ThrowIfNull(apiKeys);

        var normalizedKeys = NormalizeApiKeys(apiKeys);
        if (normalizedKeys.Count == 0)
            throw new ArgumentException("At least one API key is required.", nameof(apiKeys));

        var plaintext = JsonSerializer.Serialize(new SecretEnvelope
        {
            Version = SecretEnvelopeVersion,
            ApiKeys = normalizedKeys.ConvertAll(static apiKey => (string?)apiKey),
        });
        WriteSecretPayload(plaintext);
    }

    public void SaveGroqApiKey(string apiKey)
    {
        if (string.IsNullOrWhiteSpace(apiKey))
            throw new ArgumentException("API key cannot be empty.", nameof(apiKey));

        SaveGroqApiKeys([apiKey]);
    }

    public void ClearGroqApiKeys()
    {
        if (File.Exists(_secretPath))
            File.Delete(_secretPath);
    }

    public void ClearGroqApiKey()
    {
        ClearGroqApiKeys();
    }

    private void WriteSecretPayload(string plaintext)
    {
        var plaintextBytes = Encoding.UTF8.GetBytes(plaintext);
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

    private static IReadOnlyList<string> DeserializeEnvelope(string plaintext)
    {
        if (string.IsNullOrWhiteSpace(plaintext) || !plaintext.TrimStart().StartsWith("{", StringComparison.Ordinal))
            throw BuildLegacyFormatException();

        try
        {
            var envelope = JsonSerializer.Deserialize<SecretEnvelope>(plaintext);
            if (envelope is null ||
                envelope.Version != SecretEnvelopeVersion ||
                envelope.ApiKeys is null)
            {
                throw BuildMalformedEnvelopeException();
            }

            var normalizedKeys = NormalizeApiKeys(envelope.ApiKeys, rejectNullEntries: true);
            if (normalizedKeys.Count == 0)
                throw BuildMalformedEnvelopeException();

            return normalizedKeys;
        }
        catch (JsonException ex)
        {
            throw new InvalidOperationException(
                "Stored API keys use an unsupported format. Re-enter and save the keys in the new format.",
                ex);
        }
    }

    private static List<string> NormalizeApiKeys(IEnumerable<string?> apiKeys, bool rejectNullEntries = false)
    {
        var normalizedKeys = new List<string>();
        var seen = new HashSet<string>(StringComparer.Ordinal);

        foreach (var apiKey in apiKeys)
        {
            if (apiKey is null)
            {
                if (rejectNullEntries)
                    throw BuildMalformedEnvelopeException();
                continue;
            }

            var normalized = apiKey.Trim();
            if (string.IsNullOrWhiteSpace(normalized))
                continue;
            if (!seen.Add(normalized))
                continue;
            normalizedKeys.Add(normalized);
        }

        return normalizedKeys;
    }

    private static InvalidOperationException BuildLegacyFormatException()
    {
        return new InvalidOperationException(
            "Stored API keys use an older unsupported format. Re-enter and save the keys in the new format.");
    }

    private static InvalidOperationException BuildMalformedEnvelopeException()
    {
        return new InvalidOperationException(
            "Stored API keys use an unsupported format. Re-enter and save the keys in the new format.");
    }

    private sealed class SecretEnvelope
    {
        public int Version { get; init; }
        public List<string?>? ApiKeys { get; init; }
    }
}
