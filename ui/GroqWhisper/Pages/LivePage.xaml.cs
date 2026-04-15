using Microsoft.UI.Xaml.Controls;
using GroqWhisper.ViewModels;

namespace GroqWhisper.Pages;

public sealed partial class LivePage : Page
{
    public LiveViewModel ViewModel { get; } = new();

    public LivePage()
    {
        InitializeComponent();
    }

    private void ModelSelector_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (sender is ComboBox combo && combo.SelectedItem is ComboBoxItem item)
        {
            ViewModel.SelectedModelId = item.Tag?.ToString() ?? "whisper-large-v3-turbo";
        }
    }
}
