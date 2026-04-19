using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.ComponentModel;
using GroqWhisper.Core;
using GroqWhisper.Services;

namespace GroqWhisper.Pages;

public sealed partial class SettingsPage : Page
{
    private TranscriptionApiClient Api => App.Api ?? throw new InvalidOperationException("API client not set");
    private WindowsSecretStore SecretStore => App.SecretStore;
    private BackendStateCoordinator BackendState => App.BackendState;
    private bool IsRevealed => ApiKeysEditor.Visibility == Visibility.Visible;

    public SettingsPage()
    {
        InitializeComponent();
        Loaded += OnLoaded;
        Unloaded += OnUnloaded;
    }

    private async void OnLoaded(object sender, RoutedEventArgs e)
    {
        BackendState.PropertyChanged += OnBackendStatePropertyChanged;
        await BackendState.RefreshAsync();
        await LoadSettingsAsync();
    }

    private void OnUnloaded(object sender, RoutedEventArgs e)
    {
        BackendState.PropertyChanged -= OnBackendStatePropertyChanged;
        ClearEditorFields();
    }

    private async Task LoadSettingsAsync()
    {
        UpdateStoredKeyState();
        ApplyMutatingState();

        try
        {
            var settings = await Api.GetSettingsAsync();

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

    private void OnBackendStatePropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName is nameof(BackendStateCoordinator.CurrentState) or nameof(BackendStateCoordinator.CanMutateSettings))
            ApplyMutatingState();
    }

    private void ApplyMutatingState()
    {
        var canMutate = BackendState.CanMutateSettings;
        SaveButton.IsEnabled = canMutate;
        ClearButton.IsEnabled = canMutate;
        DefaultModelBox.IsEnabled = canMutate;
        LanguageBox.IsEnabled = canMutate;
        WindowSecondsBox.IsEnabled = canMutate;
        HopSecondsBox.IsEnabled = canMutate;
        ApiKeysEditor.IsEnabled = canMutate;
        if (canMutate)
        {
            MutatingStateText.Text = "";
        }
        else if (!BackendState.LastRefreshSucceeded)
        {
            MutatingStateText.Text = "Backend state could not be verified. Reconnect the backend before changing settings or the stored keys.";
        }
        else
        {
            MutatingStateText.Text = "Stop the active transcription session before changing settings or the stored keys.";
        }
    }

    private string GetKeyDraft()
    {
        return ApiKeysEditor.Text;
    }

    private void ClearEditorFields()
    {
        ApiKeysEditor.Text = "";
        ApiKeysEditor.Visibility = Visibility.Collapsed;
        HideButton.Visibility = Visibility.Collapsed;
        RevealButton.Visibility = Visibility.Visible;
    }

    private void UpdateStoredKeyState()
    {
        if (!SecretStore.HasGroqApiKeys())
        {
            KeyStateText.Text = "No API keys stored.";
            return;
        }

        try
        {
            var keyCount = SecretStore.LoadGroqApiKeys().Count;
            KeyStateText.Text = $"Stored API keys: {keyCount}. They remain encrypted for this Windows user.";
        }
        catch (InvalidOperationException ex)
        {
            KeyStateText.Text = ex.Message;
        }
    }

    private void Reveal_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var draft = GetKeyDraft();
            if (string.IsNullOrWhiteSpace(draft))
            {
                var storedKeys = SecretStore.LoadGroqApiKeys();
                draft = string.Join(Environment.NewLine, storedKeys);
            }

            ApiKeysEditor.Text = draft;
            ApiKeysEditor.Visibility = Visibility.Visible;
            HideButton.Visibility = Visibility.Visible;
            RevealButton.Visibility = Visibility.Collapsed;
            StatusText.Text = string.IsNullOrWhiteSpace(draft)
                ? "Editor ready. Paste one API key per line."
                : $"Revealed {GetDraftKeys().Count} stored API keys locally.";
        }
        catch (Exception ex)
        {
            StatusText.Text = ex.Message;
        }
    }

    private void Hide_Click(object sender, RoutedEventArgs e)
    {
        ClearEditorFields();
        StatusText.Text = "Revealed API keys cleared from the form.";
    }

    private async Task<bool> EnsureCanMutateSettingsAsync(string blockedMessage)
    {
        var refreshSucceeded = await BackendState.RefreshAsync();
        if (!refreshSucceeded)
        {
            StatusText.Text = "Could not verify backend state. Try again after the backend reconnects.";
            return false;
        }
        if (BackendState.CanMutateSettings)
            return true;

        StatusText.Text = blockedMessage;
        return false;
    }

    private async void ClearKey_Click(object sender, RoutedEventArgs e)
    {
        if (!await EnsureCanMutateSettingsAsync("Stop the active transcription session before clearing the stored keys."))
        {
            return;
        }

        try
        {
            SecretStore.ClearGroqApiKeys();
            ClearEditorFields();
            UpdateStoredKeyState();
            StatusText.Text = "Stored API keys cleared.";
        }
        catch (Exception ex)
        {
            StatusText.Text = ex.Message;
        }
    }

    private async void Save_Click(object sender, RoutedEventArgs e)
    {
        var storedKeysUpdated = false;
        var storedKeysUnchanged = false;
        var shouldClearEditorFields = false;
        try
        {
            if (!await EnsureCanMutateSettingsAsync("Stop the active transcription session before saving settings."))
            {
                return;
            }

            shouldClearEditorFields = true;
            var settings = new Dictionary<string, object>();
            var keyDraft = GetKeyDraft();
            if (!string.IsNullOrWhiteSpace(keyDraft))
            {
                SecretStore.SaveGroqApiKeys(GetDraftKeys());
                storedKeysUpdated = true;
                UpdateStoredKeyState();
            }
            else
            {
                storedKeysUnchanged = true;
            }

            if (DefaultModelBox.SelectedItem is ComboBoxItem modelItem)
                settings["model"] = modelItem.Tag?.ToString() ?? "whisper-large-v3-turbo";

            settings["language"] = string.IsNullOrWhiteSpace(LanguageBox.Text)
                ? null! : LanguageBox.Text;

            settings["window_seconds"] = WindowSecondsBox.Value;
            settings["hop_seconds"] = HopSecondsBox.Value;

            var result = await Api.PutSettingsAsync(settings);
            if (result.TryGetProperty("ok", out var ok) && ok.GetBoolean())
            {
                StatusText.Text = storedKeysUpdated
                    ? "API keys saved locally and settings saved."
                    : storedKeysUnchanged
                        ? "Settings saved. Stored API keys were unchanged. Use Clear to remove them."
                        : "Settings saved.";
            }
            else if (result.TryGetProperty("error", out var err))
            {
                var error = err.GetString() ?? "Settings save failed.";
                StatusText.Text = storedKeysUpdated
                    ? $"API keys saved locally, but backend settings were not applied: {error}"
                    : storedKeysUnchanged
                        ? $"{error} Stored API keys were unchanged."
                        : error;
            }
        }
        catch (Exception ex)
        {
            StatusText.Text = storedKeysUpdated
                ? $"API keys saved locally, but backend settings were not applied: {ex.Message}"
                : $"Error: {ex.Message}";
        }
        finally
        {
            if (shouldClearEditorFields)
                ClearEditorFields();
            UpdateStoredKeyState();
        }
    }

    private IReadOnlyList<string> GetDraftKeys()
    {
        return GetKeyDraft()
            .Split(["\r\n", "\n"], StringSplitOptions.None)
            .ToList();
    }
}
