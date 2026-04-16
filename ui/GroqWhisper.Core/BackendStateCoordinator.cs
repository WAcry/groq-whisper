using CommunityToolkit.Mvvm.ComponentModel;

namespace GroqWhisper.Core;

public sealed partial class BackendStateCoordinator : ObservableObject
{
    private readonly Func<CancellationToken, Task<string>>? _stateReader;

    [ObservableProperty]
    private string _currentState = "idle";

    public BackendStateCoordinator(Func<CancellationToken, Task<string>>? stateReader = null)
    {
        _stateReader = stateReader;
    }

    public bool CanMutateSettings =>
        !CurrentState.Equals("running", StringComparison.OrdinalIgnoreCase) &&
        !CurrentState.Equals("paused", StringComparison.OrdinalIgnoreCase) &&
        !CurrentState.Equals("preflight", StringComparison.OrdinalIgnoreCase);

    partial void OnCurrentStateChanged(string value)
    {
        OnPropertyChanged(nameof(CanMutateSettings));
    }

    public async Task RefreshAsync(CancellationToken cancellationToken = default)
    {
        if (_stateReader is null)
            return;

        var state = await _stateReader(cancellationToken);
        SetState(state);
    }

    public void SetState(string? state)
    {
        CurrentState = string.IsNullOrWhiteSpace(state) ? "unknown" : state.Trim().ToLowerInvariant();
    }

    public void OnStartSucceeded() => SetState("running");
    public void OnPauseSucceeded() => SetState("paused");
    public void OnResumeSucceeded() => SetState("running");
    public void OnStopSucceeded() => SetState("idle");
    public void OnError() => SetState("error");
    public void OnBackendDisconnected() => SetState("disconnected");
}
