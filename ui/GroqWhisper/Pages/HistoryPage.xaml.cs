using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using GroqWhisper.Models;
using GroqWhisper.ViewModels;

namespace GroqWhisper.Pages;

public sealed partial class HistoryPage : Page
{
    public HistoryViewModel ViewModel { get; } = new();

    public HistoryPage()
    {
        InitializeComponent();
        Loaded += async (_, _) => await ViewModel.LoadSessionsAsync();
    }

    private async void Refresh_Click(object sender, RoutedEventArgs e)
    {
        await ViewModel.LoadSessionsAsync();
    }

    private async void Delete_Click(object sender, RoutedEventArgs e)
    {
        if (sender is Button { Tag: string id })
        {
            var dialog = new ContentDialog
            {
                Title = "Delete Session",
                Content = "Are you sure you want to delete this session?",
                PrimaryButtonText = "Delete",
                CloseButtonText = "Cancel",
                XamlRoot = XamlRoot,
            };
            if (await dialog.ShowAsync() == ContentDialogResult.Primary)
            {
                await ViewModel.DeleteSessionAsync(id);
            }
        }
    }

    private async void SessionList_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (SessionList.SelectedItem is Session session)
        {
            var full = await ViewModel.GetFullSessionAsync(session.Id);
            if (full?.FullText is not null)
            {
                var dialog = new ContentDialog
                {
                    Title = $"Session {session.StartedAt}",
                    Content = new ScrollViewer
                    {
                        Content = new TextBlock
                        {
                            Text = full.FullText,
                            TextWrapping = TextWrapping.Wrap,
                            IsTextSelectionEnabled = true,
                        },
                        MaxHeight = 400,
                    },
                    CloseButtonText = "Close",
                    XamlRoot = XamlRoot,
                };
                await dialog.ShowAsync();
            }
            SessionList.SelectedItem = null;
        }
    }
}
