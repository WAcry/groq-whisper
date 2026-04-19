using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using GroqWhisper.ViewModels;

namespace GroqWhisper.Pages;

public sealed partial class LivePage : Page
{
    public LiveViewModel ViewModel { get; } = new();

    public LivePage()
    {
        InitializeComponent();
        Loaded += OnLoaded;
        App.BackendDisconnected += OnBackendDisconnected;
    }

    private async void OnLoaded(object sender, RoutedEventArgs e)
    {
        if (App.Api is not null)
            ViewModel.SetApiClient(App.Api);
        ViewModel.SetBackendStateCoordinator(App.BackendState);
        await ViewModel.LoadModelFromSettingsAsync();
    }

    private void OnBackendDisconnected()
    {
        ViewModel.HandleBackendDisconnected();
    }

    private void ModelSelector_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (sender is ComboBox combo && combo.SelectedItem is ComboBoxItem item)
        {
            ViewModel.SelectedModelId = item.Tag?.ToString() ?? "whisper-large-v3-turbo";
        }
    }
}
