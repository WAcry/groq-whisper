using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using GroqWhisper.Services;

namespace GroqWhisper.Pages;

public sealed partial class SettingsPage : Page
{
    private TranscriptionApiClient Api => App.Api ?? throw new InvalidOperationException("API client not set");

    public SettingsPage()
    {
        InitializeComponent();
        Loaded += async (_, _) => await LoadSettingsAsync();
    }

    private async Task LoadSettingsAsync()
    {
        try
        {
            var settings = await Api.GetSettingsAsync();

            if (settings.TryGetProperty("api_key_file", out var keyFile) &&
                keyFile.ValueKind != System.Text.Json.JsonValueKind.Null)
                ApiKeyPathBox.Text = keyFile.GetString();

            if (settings.TryGetProperty("model", out var model))
            {
                DefaultModelBox.SelectedIndex = model.GetString() == "whisper-large-v3" ? 1 : 0;
            }

            if (settings.TryGetProperty("language", out var lang) &&
                lang.ValueKind != System.Text.Json.JsonValueKind.Null)
                LanguageBox.Text = lang.GetString();

            if (settings.TryGetProperty("window_seconds", out var ws))
                WindowSecondsBox.Value = ws.GetDouble();

            if (settings.TryGetProperty("hop_seconds", out var hs))
                HopSecondsBox.Value = hs.GetDouble();
        }
        catch (Exception ex)
        {
            StatusText.Text = $"Failed to load settings: {ex.Message}";
        }
    }

    private async void Save_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var settings = new Dictionary<string, object>();

            settings["api_key_file"] = string.IsNullOrWhiteSpace(ApiKeyPathBox.Text)
                ? null! : ApiKeyPathBox.Text;

            if (DefaultModelBox.SelectedItem is ComboBoxItem modelItem)
                settings["model"] = modelItem.Tag?.ToString() ?? "whisper-large-v3-turbo";

            settings["language"] = string.IsNullOrWhiteSpace(LanguageBox.Text)
                ? null! : LanguageBox.Text;

            settings["window_seconds"] = WindowSecondsBox.Value;
            settings["hop_seconds"] = HopSecondsBox.Value;

            var result = await Api.PutSettingsAsync(settings);
            if (result.TryGetProperty("ok", out var ok) && ok.GetBoolean())
                StatusText.Text = "Settings saved.";
            else if (result.TryGetProperty("error", out var err))
                StatusText.Text = err.GetString();
        }
        catch (Exception ex)
        {
            StatusText.Text = $"Error: {ex.Message}";
        }
    }
}
