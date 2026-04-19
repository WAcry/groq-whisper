using GroqWhisper.Core;
using Xunit;

namespace GroqWhisper.Tests;

public sealed class BackendStateCoordinatorTests
{
    [Fact]
    public async Task RefreshUsesInjectedReader()
    {
        var coordinator = new BackendStateCoordinator(_ => Task.FromResult("running"));

        await coordinator.RefreshAsync();

        Assert.Equal("running", coordinator.CurrentState);
        Assert.False(coordinator.CanMutateSettings);
    }

    [Fact]
    public async Task RefreshFallsBackToDisconnectedWhenReaderFails()
    {
        var coordinator = new BackendStateCoordinator(_ => throw new InvalidOperationException("boom"));

        await coordinator.RefreshAsync();

        Assert.Equal("unknown", coordinator.CurrentState);
        Assert.False(coordinator.LastRefreshSucceeded);
        Assert.False(coordinator.CanMutateSettings);
    }

    [Fact]
    public async Task RefreshFailureDoesNotUnlockActiveSessionState()
    {
        var coordinator = new BackendStateCoordinator(_ => throw new InvalidOperationException("boom"));
        coordinator.OnStartSucceeded();

        await coordinator.RefreshAsync();

        Assert.Equal("running", coordinator.CurrentState);
        Assert.False(coordinator.LastRefreshSucceeded);
        Assert.False(coordinator.CanMutateSettings);
    }

    [Fact]
    public void RunningPausedAndPreflightBlockSettingsMutation()
    {
        var coordinator = new BackendStateCoordinator();

        coordinator.SetState("running");
        Assert.False(coordinator.CanMutateSettings);

        coordinator.SetState("paused");
        Assert.False(coordinator.CanMutateSettings);

        coordinator.SetState("preflight");
        Assert.False(coordinator.CanMutateSettings);
    }

    [Fact]
    public void ErrorAndDisconnectedRestoreSettingsMutation()
    {
        var coordinator = new BackendStateCoordinator();

        coordinator.OnStartSucceeded();
        Assert.False(coordinator.CanMutateSettings);

        coordinator.OnError();
        Assert.True(coordinator.CanMutateSettings);

        coordinator.OnBackendDisconnected();
        Assert.True(coordinator.CanMutateSettings);
    }

    [Fact]
    public async Task SuccessfulRefreshRestoresMutationAfterFailure()
    {
        var shouldFail = true;
        var coordinator = new BackendStateCoordinator(_ =>
        {
            if (shouldFail)
                throw new InvalidOperationException("boom");
            return Task.FromResult("idle");
        });

        await coordinator.RefreshAsync();
        Assert.False(coordinator.CanMutateSettings);

        shouldFail = false;
        await coordinator.RefreshAsync();

        Assert.True(coordinator.LastRefreshSucceeded);
        Assert.Equal("idle", coordinator.CurrentState);
        Assert.True(coordinator.CanMutateSettings);
    }
}
