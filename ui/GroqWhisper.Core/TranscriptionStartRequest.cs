using System.Text.Json.Serialization;

namespace GroqWhisper.Core;

public sealed class TranscriptionStartRequest
{
    [JsonPropertyName("model")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? Model { get; init; }

    [JsonPropertyName("language")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? Language { get; init; }

    [JsonPropertyName("prompt")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? Prompt { get; init; }

    [JsonPropertyName("api_keys")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public IReadOnlyList<string>? ApiKeys { get; init; }

    public static TranscriptionStartRequest Create(
        string? model = null,
        string? language = null,
        string? prompt = null,
        IEnumerable<string>? apiKeys = null)
    {
        return new TranscriptionStartRequest
        {
            Model = NormalizeOptionalValue(model),
            Language = NormalizeOptionalValue(language),
            Prompt = NormalizeOptionalValue(prompt),
            ApiKeys = NormalizeApiKeys(apiKeys),
        };
    }

    private static string? NormalizeOptionalValue(string? value)
    {
        return string.IsNullOrWhiteSpace(value) ? null : value.Trim();
    }

    private static IReadOnlyList<string>? NormalizeApiKeys(IEnumerable<string>? apiKeys)
    {
        if (apiKeys is null)
            return null;

        var normalizedKeys = new List<string>();
        var seen = new HashSet<string>(StringComparer.Ordinal);

        foreach (var apiKey in apiKeys)
        {
            if (string.IsNullOrWhiteSpace(apiKey))
                continue;

            var normalized = apiKey.Trim();
            if (!seen.Add(normalized))
                continue;

            normalizedKeys.Add(normalized);
        }

        return normalizedKeys.Count == 0 ? null : normalizedKeys;
    }
}
