using Microsoft.UI.Xaml;
using GroqWhisper.Services;

namespace GroqWhisper;

public partial class App : Application
{
    public static BackendService Backend { get; } = new();
    public static Window? MainWindowInstance { get; private set; }

    public App()
    {
        InitializeComponent();
    }

    protected override async void OnLaunched(LaunchActivatedEventArgs args)
    {
        MainWindowInstance = new MainWindow();
        MainWindowInstance.Closed += OnWindowClosed;
        MainWindowInstance.Activate();

        try
        {
            await Backend.LaunchAsync();
        }
        catch (Exception ex)
        {
            if (MainWindowInstance?.Content?.XamlRoot is not { } xamlRoot) return;
            var dialog = new Microsoft.UI.Xaml.Controls.ContentDialog
            {
                Title = "Backend Error",
                Content = $"Failed to start the backend service:\n{ex.Message}",
                CloseButtonText = "OK",
                XamlRoot = xamlRoot,
            };
            await dialog.ShowAsync();
        }
    }

    private void OnWindowClosed(object sender, WindowEventArgs args)
    {
        Task.Run(() => Backend.ShutdownAsync()).GetAwaiter().GetResult();
    }
}
