using System.Text.Json.Serialization;

namespace GroqWhisper.Models;

public sealed class TranscriptionPatch
{
    [JsonPropertyName("type")]
    public string Type { get; set; } = "";

    [JsonPropertyName("tick_index")]
    public int TickIndex { get; set; }

    [JsonPropertyName("replace_from_char")]
    public int ReplaceFromChar { get; set; }

    [JsonPropertyName("replacement_text")]
    public string ReplacementText { get; set; } = "";

    [JsonPropertyName("display_text")]
    public string DisplayText { get; set; } = "";

    [JsonPropertyName("committed_text")]
    public string CommittedText { get; set; } = "";

    [JsonPropertyName("tail_text")]
    public string TailText { get; set; } = "";

    [JsonPropertyName("model")]
    public string? Model { get; set; }

    [JsonPropertyName("window_start_s")]
    public double WindowStartS { get; set; }

    [JsonPropertyName("window_end_s")]
    public double WindowEndS { get; set; }

    [JsonPropertyName("audio_duration_s")]
    public double AudioDurationS { get; set; }
}
