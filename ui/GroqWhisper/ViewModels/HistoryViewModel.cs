using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using GroqWhisper.Models;
using GroqWhisper.Services;

namespace GroqWhisper.ViewModels;

public partial class HistoryViewModel : ObservableObject
{
    private readonly TranscriptionApiClient _api = new();

    public ObservableCollection<Session> Sessions { get; } = [];

    public async Task LoadSessionsAsync()
    {
        try
        {
            var sessions = await _api.GetSessionsAsync();
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
            return await _api.GetSessionAsync(id);
        }
        catch { return null; }
    }

    public async Task DeleteSessionAsync(string id)
    {
        try
        {
            if (await _api.DeleteSessionAsync(id))
            {
                var item = Sessions.FirstOrDefault(s => s.Id == id);
                if (item is not null)
                    Sessions.Remove(item);
            }
        }
        catch { }
    }
}
