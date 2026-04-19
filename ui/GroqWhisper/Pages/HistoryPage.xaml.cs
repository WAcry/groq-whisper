using Microsoft.UI.Input;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using GroqWhisper.Models;
using GroqWhisper.ViewModels;
using Windows.System;
using Windows.UI.Core;

namespace GroqWhisper.Pages;

public sealed partial class HistoryPage : Page
{
    private string? _selectionAnchorId;

    public HistoryViewModel ViewModel { get; } = new();

    public HistoryPage()
    {
        InitializeComponent();
        Loaded += async (_, _) => await ViewModel.LoadSessionsAsync();
    }

    private async void Refresh_Click(object sender, RoutedEventArgs e)
    {
        await ViewModel.LoadSessionsAsync();
        _selectionAnchorId = null;
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

    private async void DeleteSelected_Click(object sender, RoutedEventArgs e)
    {
        if (!ViewModel.CanDeleteSelected)
            return;

        var dialog = new ContentDialog
        {
            Title = "Delete Selected Sessions",
            Content = $"Are you sure you want to delete {ViewModel.SelectedCount} selected sessions?",
            PrimaryButtonText = "Delete",
            CloseButtonText = "Cancel",
            XamlRoot = XamlRoot,
        };
        if (await dialog.ShowAsync() == ContentDialogResult.Primary)
            await ViewModel.DeleteSelectedSessionsAsync();
    }

    private async void View_Click(object sender, RoutedEventArgs e)
    {
        if (sender is Button { Tag: string id })
        {
            var full = await ViewModel.GetFullSessionAsync(id);
            if (full?.FullText is not null)
            {
                var dialog = new ContentDialog
                {
                    Title = $"Session {full.StartedAt}",
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
        }
    }

    private void SelectionCheckBox_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not CheckBox { DataContext: Session session } checkBox)
            return;

        var isSelected = checkBox.IsChecked == true;
        if (IsShiftPressed() && !string.IsNullOrWhiteSpace(_selectionAnchorId))
            ViewModel.SetSelectionRange(_selectionAnchorId, session.Id, isSelected);

        _selectionAnchorId = session.Id;
    }

    private static bool IsShiftPressed()
    {
        var state = InputKeyboardSource.GetKeyStateForCurrentThread(VirtualKey.Shift);
        return state.HasFlag(CoreVirtualKeyStates.Down);
    }
}
