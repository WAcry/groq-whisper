using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using GroqWhisper.Pages;

namespace GroqWhisper;

public sealed partial class MainWindow : Window
{
    public MainWindow()
    {
        InitializeComponent();
        ExtendsContentIntoTitleBar = true;
        ContentFrame.Navigate(typeof(LivePage));
        NavView.SelectedItem = NavView.MenuItems[0];
    }

    private void NavView_SelectionChanged(NavigationView sender, NavigationViewSelectionChangedEventArgs args)
    {
        if (args.SelectedItemContainer is NavigationViewItem item)
        {
            var tag = item.Tag?.ToString();
            var pageType = tag switch
            {
                "Live" => typeof(LivePage),
                "History" => typeof(HistoryPage),
                "Devices" => typeof(DevicesPage),
                "Settings" => typeof(SettingsPage),
                _ => typeof(LivePage),
            };
            ContentFrame.Navigate(pageType);
        }
    }
}
