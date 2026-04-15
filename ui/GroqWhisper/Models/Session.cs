using System.Text.Json.Serialization;

namespace GroqWhisper.Models;

public sealed class Session
{
    [JsonPropertyName("id")]
    public string Id { get; set; } = "";

    [JsonPropertyName("started_at")]
    public string StartedAt { get; set; } = "";

    [JsonPropertyName("ended_at")]
    public string? EndedAt { get; set; }

    [JsonPropertyName("model")]
    public string Model { get; set; } = "";

    [JsonPropertyName("language")]
    public string? Language { get; set; }

    [JsonPropertyName("full_text")]
    public string? FullText { get; set; }

    [JsonPropertyName("text_preview")]
    public string? TextPreview { get; set; }

    [JsonPropertyName("error_log")]
    public string? ErrorLog { get; set; }

    [JsonPropertyName("duration_seconds")]
    public double? DurationSeconds { get; set; }

    [JsonPropertyName("tick_count")]
    public int TickCount { get; set; }

    [JsonPropertyName("export_path")]
    public string? ExportPath { get; set; }
}
