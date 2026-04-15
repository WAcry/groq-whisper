using Microsoft.UI.Xaml;
using GroqWhisper.Services;

namespace GroqWhisper;

public partial class App : Application
{
    public static BackendService Backend { get; } = new();

    private Window? _window;

    public App()
    {
        InitializeComponent();
    }

    protected override async void OnLaunched(LaunchActivatedEventArgs args)
    {
        _window = new MainWindow();
        _window.Closed += OnWindowClosed;
        _window.Activate();

        await Backend.LaunchAsync();
    }

    private async void OnWindowClosed(object sender, WindowEventArgs args)
    {
        await Backend.ShutdownAsync();
    }
}
