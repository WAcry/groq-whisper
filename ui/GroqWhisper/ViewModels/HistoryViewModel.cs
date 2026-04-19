using System.Collections.ObjectModel;
using System.ComponentModel;
using CommunityToolkit.Mvvm.ComponentModel;
using GroqWhisper.Models;
using GroqWhisper.Services;

namespace GroqWhisper.ViewModels;

public partial class HistoryViewModel : ObservableObject
{
    private bool _isBulkUpdatingSelection;

    private TranscriptionApiClient Api => App.Api ?? throw new InvalidOperationException("API client not set");

    public ObservableCollection<Session> Sessions { get; } = [];
    public int SelectedCount => Sessions.Count(s => s.IsSelected);
    public bool CanDeleteSelected => SelectedCount > 0;
    public string DeleteSelectedLabel => SelectedCount > 0
        ? $"Delete Selected ({SelectedCount})"
        : "Delete Selected";

    public async Task LoadSessionsAsync()
    {
        try
        {
            UnsubscribeFromSessions();
            var sessions = await Api.GetSessionsAsync();
            Sessions.Clear();
            foreach (var s in sessions)
            {
                s.PropertyChanged += Session_PropertyChanged;
                Sessions.Add(s);
            }
            NotifySelectionStateChanged();
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
                {
                    item.PropertyChanged -= Session_PropertyChanged;
                    Sessions.Remove(item);
                    NotifySelectionStateChanged();
                }
            }
        }
        catch { }
    }

    public async Task<int> DeleteSelectedSessionsAsync()
    {
        var ids = Sessions
            .Where(s => s.IsSelected)
            .Select(s => s.Id)
            .ToList();

        var deleted = 0;
        foreach (var id in ids)
        {
            try
            {
                if (await Api.DeleteSessionAsync(id))
                {
                    var item = Sessions.FirstOrDefault(s => s.Id == id);
                    if (item is not null)
                    {
                        item.PropertyChanged -= Session_PropertyChanged;
                        Sessions.Remove(item);
                    }
                    deleted++;
                }
            }
            catch { }
        }

        NotifySelectionStateChanged();
        return deleted;
    }

    public void SetSelectionRange(string anchorId, string currentId, bool isSelected)
    {
        var anchorIndex = IndexOfSession(anchorId);
        var currentIndex = IndexOfSession(currentId);
        if (anchorIndex < 0 || currentIndex < 0)
            return;

        var start = Math.Min(anchorIndex, currentIndex);
        var end = Math.Max(anchorIndex, currentIndex);

        _isBulkUpdatingSelection = true;
        try
        {
            for (var i = start; i <= end; i++)
                Sessions[i].IsSelected = isSelected;
        }
        finally
        {
            _isBulkUpdatingSelection = false;
        }

        NotifySelectionStateChanged();
    }

    private void UnsubscribeFromSessions()
    {
        foreach (var session in Sessions)
            session.PropertyChanged -= Session_PropertyChanged;
    }

    private int IndexOfSession(string id)
    {
        for (var i = 0; i < Sessions.Count; i++)
        {
            if (Sessions[i].Id == id)
                return i;
        }

        return -1;
    }

    private void Session_PropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName == nameof(Session.IsSelected) && !_isBulkUpdatingSelection)
            NotifySelectionStateChanged();
    }

    private void NotifySelectionStateChanged()
    {
        OnPropertyChanged(nameof(SelectedCount));
        OnPropertyChanged(nameof(CanDeleteSelected));
        OnPropertyChanged(nameof(DeleteSelectedLabel));
    }
}
