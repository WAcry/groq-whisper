using System.Net.Http.Json;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;
using GroqWhisper.Models;

namespace GroqWhisper.Services;

public sealed class TranscriptionApiClient
{
    private readonly HttpClient _http;

    public TranscriptionApiClient(string baseUrl = "http://127.0.0.1:8000")
    {
        _http = new HttpClient { BaseAddress = new Uri(baseUrl) };
    }

    public async Task<JsonElement> PostStartAsync(
        string? model = null,
        string? language = null,
        string? prompt = null)
    {
        var body = new Dictionary<string, string>();
        if (model is not null) body["model"] = model;
        if (language is not null) body["language"] = language;
        if (prompt is not null) body["prompt"] = prompt;

        var content = body.Count > 0
            ? new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json")
            : null;
        var response = await _http.PostAsync("/start", content);
        return await response.Content.ReadFromJsonAsync<JsonElement>();
    }

    public async Task<JsonElement> PostStopAsync()
    {
        var response = await _http.PostAsync("/stop", null);
        return await response.Content.ReadFromJsonAsync<JsonElement>();
    }

    public async Task<JsonElement> PostPauseAsync()
    {
        var response = await _http.PostAsync("/pause", null);
        return await response.Content.ReadFromJsonAsync<JsonElement>();
    }

    public async Task<JsonElement> PostResumeAsync()
    {
        var response = await _http.PostAsync("/resume", null);
        return await response.Content.ReadFromJsonAsync<JsonElement>();
    }

    public async Task<JsonElement> GetStateAsync()
    {
        return await _http.GetFromJsonAsync<JsonElement>("/state");
    }

    public async Task<JsonElement> GetDevicesAsync()
    {
        return await _http.GetFromJsonAsync<JsonElement>("/devices");
    }

    public async Task<JsonElement> GetSettingsAsync()
    {
        return await _http.GetFromJsonAsync<JsonElement>("/settings");
    }

    public async Task<JsonElement> PutSettingsAsync(Dictionary<string, object> settings)
    {
        var content = new StringContent(
            JsonSerializer.Serialize(settings), Encoding.UTF8, "application/json");
        var response = await _http.PutAsync("/settings", content);
        return await response.Content.ReadFromJsonAsync<JsonElement>();
    }

    public async Task<List<Session>> GetSessionsAsync(int limit = 50, int offset = 0)
    {
        var result = await _http.GetFromJsonAsync<JsonElement>($"/sessions?limit={limit}&offset={offset}");
        var sessions = result.GetProperty("sessions");
        return JsonSerializer.Deserialize<List<Session>>(sessions.GetRawText()) ?? [];
    }

    public async Task<Session?> GetSessionAsync(string id)
    {
        try
        {
            return await _http.GetFromJsonAsync<Session>($"/sessions/{id}");
        }
        catch (HttpRequestException ex) when (ex.StatusCode == System.Net.HttpStatusCode.NotFound)
        {
            return null;
        }
    }

    public async Task<bool> DeleteSessionAsync(string id)
    {
        var response = await _http.DeleteAsync($"/sessions/{id}");
        return response.IsSuccessStatusCode;
    }

    public async Task PatchSessionExportPathAsync(string id, string exportPath)
    {
        var body = new Dictionary<string, string> { ["export_path"] = exportPath };
        var content = new StringContent(
            JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");
        await _http.PatchAsync($"/sessions/{id}", content);
    }

    public async IAsyncEnumerable<SseEvent> SubscribeEventsAsync(
        [EnumeratorCancellation] CancellationToken cancellationToken = default)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, "/events");
        using var response = await _http.SendAsync(
            request, HttpCompletionOption.ResponseHeadersRead, cancellationToken);
        response.EnsureSuccessStatusCode();

        using var stream = await response.Content.ReadAsStreamAsync(cancellationToken);
        using var reader = new StreamReader(stream);

        string? eventType = null;
        var dataLines = new StringBuilder();

        while (!cancellationToken.IsCancellationRequested)
        {
            var line = await reader.ReadLineAsync(cancellationToken);
            if (line is null) break;

            if (line.StartsWith("event: "))
            {
                eventType = line[7..];
            }
            else if (line.StartsWith("data: "))
            {
                dataLines.Append(line[6..]);
            }
            else if (line == "")
            {
                if (dataLines.Length > 0)
                {
                    yield return new SseEvent(eventType ?? "message", dataLines.ToString());
                    eventType = null;
                    dataLines.Clear();
                }
            }
        }
    }
}

public sealed record SseEvent(string EventType, string Data)
{
    public T? Deserialize<T>() => JsonSerializer.Deserialize<T>(Data);
}
