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
}
