using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using GroqWhisper.Services;

namespace GroqWhisper.Pages;

public sealed partial class DevicesPage : Page
{
    private TranscriptionApiClient Api => App.Api ?? throw new InvalidOperationException("API client not set");

    public DevicesPage()
    {
        InitializeComponent();
        Loaded += async (_, _) => await LoadDevicesAsync();
    }

    private async void Refresh_Click(object sender, RoutedEventArgs e)
    {
        await LoadDevicesAsync();
    }

    private async Task LoadDevicesAsync()
    {
        try
        {
            var result = await Api.GetDevicesAsync();
            if (result.TryGetProperty("devices", out var devArray))
            {
                var devices = new List<DeviceDisplay>();
                foreach (var dev in devArray.EnumerateArray())
                {
                    devices.Add(new DeviceDisplay
                    {
                        Name = dev.GetProperty("name").GetString() ?? "Unknown",
                        SampleRate = $"{dev.GetProperty("sample_rate").GetInt32()} Hz",
                    });
                }
                DeviceList.ItemsSource = devices;
            }
        }
        catch { }
    }

    private sealed class DeviceDisplay
    {
        public string Name { get; set; } = "";
        public string SampleRate { get; set; } = "";
    }
}
