using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Microsoft.UI.Dispatching;
using GroqWhisper.Models;
using GroqWhisper.Services;

namespace GroqWhisper.ViewModels;

public partial class LiveViewModel : ObservableObject
{
    private readonly TranscriptionApiClient _api = new();
    private CancellationTokenSource? _eventCts;

    [ObservableProperty] private string _committedText = "";
    [ObservableProperty] private string _tailText = "";
    [ObservableProperty] private string _stateDisplay = "Idle";
    [ObservableProperty] private string _modelDisplay = "whisper-large-v3-turbo";
    [ObservableProperty] private string _durationDisplay = "00:00";
    [ObservableProperty] private int _tickCount;
    [ObservableProperty] private string _errorMessage = "";
    [ObservableProperty] private bool _hasError;
    [ObservableProperty] private int _selectedModelIndex;

    private ServiceState _currentState = ServiceState.Idle;
    public string SelectedModelId { get; set; } = "whisper-large-v3-turbo";

    [RelayCommand]
    private async Task StartAsync()
    {
        try
        {
            var result = await _api.PostStartAsync(model: SelectedModelId);
            if (result.TryGetProperty("ok", out var ok) && ok.GetBoolean())
            {
                _currentState = ServiceState.Running;
                StateDisplay = "Running";
                ModelDisplay = SelectedModelId;
                StartEventStream();
            }
            else if (result.TryGetProperty("error", out var err))
            {
                ErrorMessage = err.GetString() ?? "Start failed";
                HasError = true;
            }
        }
        catch (Exception ex)
        {
            ErrorMessage = ex.Message;
            HasError = true;
        }
    }

    [RelayCommand]
    private async Task PauseAsync()
    {
        try
        {
            var result = await _api.PostPauseAsync();
            if (result.TryGetProperty("ok", out var ok) && ok.GetBoolean())
            {
                _currentState = ServiceState.Paused;
                StateDisplay = "Paused";
            }
        }
        catch (Exception ex) { ErrorMessage = ex.Message; HasError = true; }
    }

    [RelayCommand]
    private async Task StopAsync()
    {
        try
        {
            StopEventStream();
            var result = await _api.PostStopAsync();
            if (result.TryGetProperty("ok", out var ok) && ok.GetBoolean())
            {
                _currentState = ServiceState.Idle;
                StateDisplay = "Idle";
            }
        }
        catch (Exception ex) { ErrorMessage = ex.Message; HasError = true; }
    }

    [RelayCommand]
    private void Copy()
    {
        var text = CommittedText + TailText;
        if (string.IsNullOrEmpty(text)) return;
        var package = new Windows.ApplicationModel.DataTransfer.DataPackage();
        package.SetText(text);
        Windows.ApplicationModel.DataTransfer.Clipboard.SetContent(package);
    }

    [RelayCommand]
    private async Task ExportAsync()
    {
        var text = CommittedText + TailText;
        if (string.IsNullOrEmpty(text)) return;

        var picker = new Windows.Storage.Pickers.FileSavePicker();
        picker.SuggestedStartLocation = Windows.Storage.Pickers.PickerLocationId.DocumentsLibrary;
        picker.FileTypeChoices.Add("Text", [".txt"]);
        picker.SuggestedFileName = $"transcription_{DateTime.Now:yyyyMMdd_HHmmss}";

        var file = await picker.PickSaveFileAsync();
        if (file is not null)
        {
            await Windows.Storage.FileIO.WriteTextAsync(file, text);
        }
    }

    private void StartEventStream()
    {
        StopEventStream();
        _eventCts = new CancellationTokenSource();
        _ = ProcessEventsAsync(_eventCts.Token);
    }

    private void StopEventStream()
    {
        _eventCts?.Cancel();
        _eventCts?.Dispose();
        _eventCts = null;
    }

    private async Task ProcessEventsAsync(CancellationToken ct)
    {
        try
        {
            await foreach (var evt in _api.SubscribeEventsAsync(ct))
            {
                switch (evt.EventType)
                {
                    case "transcription.patch":
                    case "transcription.final":
                        var patch = evt.Deserialize<TranscriptionPatch>();
                        if (patch is not null)
                        {
                            CommittedText = patch.CommittedText;
                            TailText = string.IsNullOrEmpty(patch.TailText) ? "" : " " + patch.TailText;
                            TickCount = patch.TickIndex;
                            var seconds = (int)patch.WindowEndS;
                            DurationDisplay = $"{seconds / 60:D2}:{seconds % 60:D2}";
                        }
                        break;

                    case "service.error":
                        var error = evt.Deserialize<Dictionary<string, string>>();
                        ErrorMessage = error?.GetValueOrDefault("message") ?? "Unknown error";
                        HasError = true;
                        _currentState = ServiceState.Error;
                        StateDisplay = "Error";
                        break;

                    case "service.paused":
                        _currentState = ServiceState.Paused;
                        StateDisplay = "Paused";
                        break;

                    case "service.resumed":
                        _currentState = ServiceState.Running;
                        StateDisplay = "Running";
                        break;
                }
            }
        }
        catch (OperationCanceledException) { }
        catch (Exception ex)
        {
            ErrorMessage = $"Event stream error: {ex.Message}";
            HasError = true;
        }
    }
}
