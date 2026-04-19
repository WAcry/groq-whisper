using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using GroqWhisper.Core;
using GroqWhisper.Models;
using GroqWhisper.Services;

namespace GroqWhisper.ViewModels;

public partial class LiveViewModel : ObservableObject
{
    private TranscriptionApiClient? _api;
    private BackendStateCoordinator? _backendState;
    private readonly DispatcherQueue _dispatcher;
    private CancellationTokenSource? _eventCts;

    [ObservableProperty] private string _committedText = "";
    [ObservableProperty] private string _tailText = "";
    [ObservableProperty] private string _displayText = "";
    [ObservableProperty] private string _stateDisplay = "Idle";
    [ObservableProperty] private string _modelDisplay = "whisper-large-v3-turbo";
    [ObservableProperty] private string _durationDisplay = "00:00";
    [ObservableProperty] private int _tickCount;
    [ObservableProperty] private string _tickCountDisplay = "0";
    [ObservableProperty] private string _errorMessage = "";
    [ObservableProperty] private Visibility _errorVisibility = Visibility.Collapsed;
    [ObservableProperty] private int _selectedModelIndex;

    private ServiceState _currentState = ServiceState.Idle;
    private string? _currentSessionId;
    public string SelectedModelId { get; set; } = "whisper-large-v3-turbo";

    public LiveViewModel()
    {
        _dispatcher = DispatcherQueue.GetForCurrentThread();
    }

    public void SetApiClient(TranscriptionApiClient api)
    {
        _api = api;
    }

    public void SetBackendStateCoordinator(BackendStateCoordinator coordinator)
    {
        _backendState = coordinator;
    }
    partial void OnTickCountChanged(int value)
    {
        TickCountDisplay = value.ToString();
    }

    private TranscriptionApiClient Api => _api ?? throw new InvalidOperationException("API client not set");

    public async Task LoadModelFromSettingsAsync()
    {
        for (int attempt = 0; attempt < 5; attempt++)
        {
            try
            {
                var settings = await Api.GetSettingsAsync();
                if (settings.TryGetProperty("model", out var model))
                {
                    SelectedModelId = model.GetString() ?? "whisper-large-v3-turbo";
                    SelectedModelIndex = SelectedModelId == "whisper-large-v3" ? 1 : 0;
                    ModelDisplay = SelectedModelId;
                }
                return;
            }
            catch
            {
                await Task.Delay(1000);
            }
        }
    }

    [RelayCommand]
    private async Task StartAsync()
    {
        try
        {
            var apiKey = App.SecretStore.LoadGroqApiKey();
            if (string.IsNullOrWhiteSpace(apiKey))
            {
                ErrorMessage = "No Groq API key is stored. Open Settings and save a key first.";
                ErrorVisibility = Visibility.Visible;
                return;
            }

            var result = await Api.PostStartAsync(model: SelectedModelId, apiKey: apiKey);
            if (result.TryGetProperty("ok", out var ok) && ok.GetBoolean())
            {
                _currentState = ServiceState.Running;
                StateDisplay = "Running";
                ModelDisplay = SelectedModelId;
                _backendState?.OnStartSucceeded();
                if (result.TryGetProperty("session_id", out var sid))
                    _currentSessionId = sid.GetString();
                StartEventStream();
            }
            else if (result.TryGetProperty("error", out var err))
            {
                ErrorMessage = err.GetString() ?? "Start failed";
                ErrorVisibility = Visibility.Visible;
            }
        }
        catch (Exception ex)
        {
            ErrorMessage = ex.Message;
            ErrorVisibility = Visibility.Visible;
        }
    }

    [RelayCommand]
    private async Task PauseAsync()
    {
        try
        {
            var result = await Api.PostPauseAsync();
            if (result.TryGetProperty("ok", out var ok) && ok.GetBoolean())
            {
                _currentState = ServiceState.Paused;
                StateDisplay = "Paused";
                _backendState?.OnPauseSucceeded();
            }
            else if (result.TryGetProperty("error", out var err))
            {
                ErrorMessage = err.GetString() ?? "Pause failed";
                ErrorVisibility = Visibility.Visible;
            }
        }
        catch (Exception ex) { ErrorMessage = ex.Message; ErrorVisibility = Visibility.Visible; }
    }

    [RelayCommand]
    private async Task ResumeAsync()
    {
        try
        {
            var result = await Api.PostResumeAsync();
            if (result.TryGetProperty("ok", out var ok) && ok.GetBoolean())
            {
                _currentState = ServiceState.Running;
                StateDisplay = "Running";
                _backendState?.OnResumeSucceeded();
                if (result.TryGetProperty("session_id", out var sid))
                    _currentSessionId = sid.GetString();
            }
            else if (result.TryGetProperty("error", out var err))
            {
                ErrorMessage = err.GetString() ?? "Resume failed";
                ErrorVisibility = Visibility.Visible;
            }
        }
        catch (Exception ex) { ErrorMessage = ex.Message; ErrorVisibility = Visibility.Visible; }
    }

    [RelayCommand]
    private async Task StopAsync()
    {
        try
        {
            var result = await Api.PostStopAsync();
            if (result.TryGetProperty("ok", out var ok) && ok.GetBoolean())
            {
                _currentState = ServiceState.Idle;
                StateDisplay = "Idle";
                _backendState?.OnStopSucceeded();
            }
            else if (result.TryGetProperty("error", out var err))
            {
                ErrorMessage = err.GetString() ?? "Stop failed";
                ErrorVisibility = Visibility.Visible;
                if (result.TryGetProperty("state", out var stateValue))
                {
                    var state = ServiceStateExtensions.Parse(stateValue.GetString());
                    if (state == ServiceState.Error)
                    {
                        _currentState = ServiceState.Error;
                        StateDisplay = "Error";
                        _backendState?.OnError();
                    }
                }
            }
            await Task.Delay(500);
            StopEventStream();
        }
        catch (Exception ex) { ErrorMessage = ex.Message; ErrorVisibility = Visibility.Visible; }
    }

    [RelayCommand]
    private void Copy()
    {
        var text = DisplayText;
        if (string.IsNullOrEmpty(text)) return;
        var package = new Windows.ApplicationModel.DataTransfer.DataPackage();
        package.SetText(text);
        Windows.ApplicationModel.DataTransfer.Clipboard.SetContent(package);
    }

    [RelayCommand]
    private async Task ExportAsync()
    {
        var text = DisplayText;
        if (string.IsNullOrEmpty(text)) return;

        var picker = new Windows.Storage.Pickers.FileSavePicker();
        picker.SuggestedStartLocation = Windows.Storage.Pickers.PickerLocationId.DocumentsLibrary;
        picker.FileTypeChoices.Add("Text", [".txt"]);
        picker.SuggestedFileName = $"transcription_{DateTime.Now:yyyyMMdd_HHmmss}";

        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(App.MainWindowInstance);
        WinRT.Interop.InitializeWithWindow.Initialize(picker, hwnd);

        var file = await picker.PickSaveFileAsync();
        if (file is not null)
        {
            await Windows.Storage.FileIO.WriteTextAsync(file, text);
            if (_currentSessionId is not null)
            {
                try
                {
                    await Api.PatchSessionExportPathAsync(_currentSessionId, file.Path);
                }
                catch { }
            }
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

    public void HandleBackendDisconnected()
    {
        StopEventStream();
        _currentState = ServiceState.Error;
        StateDisplay = "Backend Disconnected";
        _backendState?.OnBackendDisconnected();
        ErrorMessage = "Backend process exited unexpectedly";
        ErrorVisibility = Visibility.Visible;
    }

    private async Task ProcessEventsAsync(CancellationToken ct)
    {
        try
        {
            var api = _api ?? throw new InvalidOperationException("API client not set");
            await foreach (var evt in api.SubscribeEventsAsync(ct))
            {
                _dispatcher.TryEnqueue(() =>
                {
                    switch (evt.EventType)
                    {
                        case "transcription.patch":
                        case "transcription.final":
                            var patch = evt.Deserialize<TranscriptionPatch>();
                            if (patch is not null)
                            {
                                CommittedText = patch.CommittedText;
                                DisplayText = patch.DisplayText;
                                TailText = patch.DisplayText.Length > patch.CommittedText.Length
                                    ? patch.DisplayText[patch.CommittedText.Length..]
                                    : "";
                                TickCount = patch.TickIndex;
                                var seconds = (int)patch.WindowEndS;
                                DurationDisplay = $"{seconds / 60:D2}:{seconds % 60:D2}";
                            }
                            break;

                        case "service.error":
                            var error = evt.Deserialize<Dictionary<string, string>>();
                            ErrorMessage = error?.GetValueOrDefault("message") ?? "Unknown error";
                            ErrorVisibility = Visibility.Visible;
                            _currentState = ServiceState.Error;
                            StateDisplay = "Error";
                            _backendState?.OnError();
                            break;

                        case "service.paused":
                            _currentState = ServiceState.Paused;
                            StateDisplay = "Paused";
                            _backendState?.OnPauseSucceeded();
                            break;

                        case "service.resumed":
                            _currentState = ServiceState.Running;
                            StateDisplay = "Running";
                            _backendState?.OnResumeSucceeded();
                            var resumed = evt.Deserialize<Dictionary<string, string>>();
                            if (resumed?.TryGetValue("session_id", out var newSid) == true)
                                _currentSessionId = newSid;
                            break;
                    }
                });
            }

            if (!ct.IsCancellationRequested && _currentState == ServiceState.Running)
            {
                _dispatcher.TryEnqueue(() =>
                {
                    ErrorMessage = "Backend connection lost";
                    ErrorVisibility = Visibility.Visible;
                    _currentState = ServiceState.Error;
                    StateDisplay = "Disconnected";
                    _backendState?.OnBackendDisconnected();
                });
            }
        }
        catch (OperationCanceledException) { }
        catch (Exception ex)
        {
            _dispatcher.TryEnqueue(() =>
            {
                ErrorMessage = $"Event stream error: {ex.Message}";
                ErrorVisibility = Visibility.Visible;
                _currentState = ServiceState.Error;
                StateDisplay = "Disconnected";
                _backendState?.OnBackendDisconnected();
            });
        }
    }
}
