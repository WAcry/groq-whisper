using CommunityToolkit.Mvvm.ComponentModel;

namespace GroqWhisper.Core;

public sealed partial class BackendStateCoordinator : ObservableObject
{
    private readonly Func<CancellationToken, Task<string>>? _stateReader;

    [ObservableProperty]
    private string _currentState = "idle";

    [ObservableProperty]
    private bool _lastRefreshSucceeded = true;

    public BackendStateCoordinator(Func<CancellationToken, Task<string>>? stateReader = null)
    {
        _stateReader = stateReader;
    }

    public bool CanMutateSettings =>
        LastRefreshSucceeded &&
        !CurrentState.Equals("running", StringComparison.OrdinalIgnoreCase) &&
        !CurrentState.Equals("paused", StringComparison.OrdinalIgnoreCase) &&
        !CurrentState.Equals("preflight", StringComparison.OrdinalIgnoreCase);

    partial void OnCurrentStateChanged(string value)
    {
        OnPropertyChanged(nameof(CanMutateSettings));
    }

    partial void OnLastRefreshSucceededChanged(bool value)
    {
        OnPropertyChanged(nameof(CanMutateSettings));
    }

    public async Task<bool> RefreshAsync(CancellationToken cancellationToken = default)
    {
        if (_stateReader is null)
            return false;

        try
        {
            var state = await _stateReader(cancellationToken);
            SetKnownState(state);
            return true;
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch
        {
            LastRefreshSucceeded = false;
            if (!IsActiveSessionState(CurrentState))
                CurrentState = "unknown";
            return false;
        }
    }

    public void SetState(string? state)
    {
        SetKnownState(state);
    }

    private void SetKnownState(string? state)
    {
        LastRefreshSucceeded = true;
        CurrentState = string.IsNullOrWhiteSpace(state) ? "unknown" : state.Trim().ToLowerInvariant();
    }

    private static bool IsActiveSessionState(string? state)
    {
        return state is not null && (
            state.Equals("running", StringComparison.OrdinalIgnoreCase) ||
            state.Equals("paused", StringComparison.OrdinalIgnoreCase) ||
            state.Equals("preflight", StringComparison.OrdinalIgnoreCase));
    }

    public void OnStartSucceeded() => SetState("running");
    public void OnPauseSucceeded() => SetState("paused");
    public void OnResumeSucceeded() => SetState("running");
    public void OnStopSucceeded() => SetState("idle");
    public void OnError() => SetState("error");
    public void OnBackendDisconnected() => SetState("disconnected");
}
