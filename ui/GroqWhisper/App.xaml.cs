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
            var dialog = new Microsoft.UI.Xaml.Controls.ContentDialog
            {
                Title = "Backend Error",
                Content = $"Failed to start the backend service:\n{ex.Message}",
                CloseButtonText = "OK",
                XamlRoot = MainWindowInstance.Content.XamlRoot,
            };
            await dialog.ShowAsync();
        }
    }

    private async void OnWindowClosed(object sender, WindowEventArgs args)
    {
        await Backend.ShutdownAsync();
    }
}
