namespace GroqWhisper.Core;

public sealed record TranscriptionStartPreparationResult(
    TranscriptionStartRequest? Request,
    string? ErrorMessage);

public static class TranscriptionStartPreparation
{
    public static TranscriptionStartPreparationResult Create(
        WindowsSecretStore secretStore,
        string? model = null,
        string? language = null,
        string? prompt = null)
    {
        ArgumentNullException.ThrowIfNull(secretStore);

        var apiKeys = secretStore.LoadGroqApiKeys();
        if (apiKeys.Count == 0)
        {
            return new TranscriptionStartPreparationResult(
                Request: null,
                ErrorMessage: "No Groq API keys are stored. Open Settings and save at least one key first.");
        }

        return new TranscriptionStartPreparationResult(
            Request: TranscriptionStartRequest.Create(
                model: model,
                language: language,
                prompt: prompt,
                apiKeys: apiKeys),
            ErrorMessage: null);
    }
}
