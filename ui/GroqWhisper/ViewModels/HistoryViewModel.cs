using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using GroqWhisper.Models;
using GroqWhisper.Services;

namespace GroqWhisper.ViewModels;

public partial class HistoryViewModel : ObservableObject
{
    private TranscriptionApiClient Api => App.Api ?? throw new InvalidOperationException("API client not set");

    public ObservableCollection<Session> Sessions { get; } = [];

    public async Task LoadSessionsAsync()
    {
        try
        {
            var sessions = await Api.GetSessionsAsync();
            Sessions.Clear();
            foreach (var s in sessions)
                Sessions.Add(s);
        }
        catch { }
    }

    public async Task<Session?> GetFullSessionAsync(string id)
    {
        try
        {
            return await Api.GetSessionAsync(id);
        }
        catch { return null; }
    }

    public async Task DeleteSessionAsync(string id)
    {
        try
        {
            if (await Api.DeleteSessionAsync(id))
            {
                var item = Sessions.FirstOrDefault(s => s.Id == id);
                if (item is not null)
                    Sessions.Remove(item);
            }
        }
        catch { }
    }
}
