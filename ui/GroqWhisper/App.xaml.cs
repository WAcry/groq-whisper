using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using GroqWhisper.Core;
using GroqWhisper.Services;

namespace GroqWhisper;

public partial class App : Application
{
    public static BackendService Backend { get; } = new();
    public static TranscriptionApiClient? Api { get; set; }
    public static WindowsSecretStore SecretStore { get; } = new();
    public static BackendStateCoordinator BackendState { get; } = new(
        async cancellationToken =>
        {
            if (Api is null)
                return "disconnected";
            var state = await Api.GetStateAsync(cancellationToken);
            return state.TryGetProperty("state", out var value)
                ? value.GetString() ?? "unknown"
                : "unknown";
        });
    public static Window? MainWindowInstance { get; private set; }
    public static event Action? BackendDisconnected;

    public App()
    {
        InitializeComponent();
    }

    protected override async void OnLaunched(LaunchActivatedEventArgs args)
    {
        try
        {
            await Backend.LaunchAsync();
            Api = new TranscriptionApiClient(Backend.BaseUrl);
            await BackendState.RefreshAsync();
        }
        catch (Exception ex)
        {
            MainWindowInstance = new MainWindow();
            MainWindowInstance.Activate();
            if (MainWindowInstance.Content?.XamlRoot is { } xamlRoot)
            {
                var dialog = new Microsoft.UI.Xaml.Controls.ContentDialog
                {
                    Title = "Backend Error",
                    Content = $"Failed to start the backend service:\n{ex.Message}\n\nThe application will close.",
                    CloseButtonText = "OK",
                    XamlRoot = xamlRoot,
                };
                await dialog.ShowAsync();
            }
            MainWindowInstance.Close();
            return;
        }

        Backend.BackendExited += OnBackendExited;

        MainWindowInstance = new MainWindow();
        MainWindowInstance.Closed += OnWindowClosed;
        MainWindowInstance.Activate();
    }

    private void OnBackendExited(int exitCode)
    {
        var dispatcher = MainWindowInstance?.DispatcherQueue;
        dispatcher?.TryEnqueue(() =>
        {
            Api = null;
            BackendState.OnBackendDisconnected();
            BackendDisconnected?.Invoke();
        });
    }

    private void OnWindowClosed(object sender, WindowEventArgs args)
    {
        Task.Run(() => Backend.ShutdownAsync()).GetAwaiter().GetResult();
    }
}
