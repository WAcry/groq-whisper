using System.Diagnostics;

namespace GroqWhisper.Services;

public sealed class BackendService
{
    private const string DefaultBaseUrl = "http://127.0.0.1:8000";
    private const int HealthCheckIntervalMs = 500;
    private const int HealthCheckTimeoutMs = 15_000;

    private Process? _process;
    private readonly HttpClient _http = new() { BaseAddress = new Uri(DefaultBaseUrl) };

    public bool IsRunning => _process is { HasExited: false };

    public async Task LaunchAsync(string? pythonPath = null, string? servePath = null)
    {
        var python = pythonPath ?? "python";
        var serve = servePath ?? FindServePath();

        var startInfo = new ProcessStartInfo
        {
            FileName = python,
            Arguments = $"\"{serve}\"",
            WorkingDirectory = Path.GetDirectoryName(serve) ?? ".",
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };

        _process = Process.Start(startInfo);
        if (_process is null)
            throw new InvalidOperationException("Failed to start backend process.");

        _process.OutputDataReceived += (_, e) => Debug.WriteLine($"[backend] {e.Data}");
        _process.ErrorDataReceived += (_, e) => Debug.WriteLine($"[backend:err] {e.Data}");
        _process.BeginOutputReadLine();
        _process.BeginErrorReadLine();

        await WaitForReadyAsync(TimeSpan.FromMilliseconds(HealthCheckTimeoutMs));
    }

    public async Task WaitForReadyAsync(TimeSpan timeout)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            try
            {
                var response = await _http.GetAsync("/healthz");
                if (response.IsSuccessStatusCode)
                    return;
            }
            catch (HttpRequestException) { }
            catch (TaskCanceledException) { }

            await Task.Delay(HealthCheckIntervalMs);
        }

        throw new TimeoutException("Backend did not become ready within the timeout period.");
    }

    public async Task ShutdownAsync()
    {
        if (_process is null || _process.HasExited)
            return;

        try
        {
            await _http.PostAsync("/stop", null);
        }
        catch { }

        try
        {
            if (!_process.HasExited)
            {
                _process.Kill(entireProcessTree: true);
                await _process.WaitForExitAsync();
            }
        }
        catch { }
        finally
        {
            _process?.Dispose();
            _process = null;
        }
    }

    private static string FindServePath()
    {
        var candidates = new[]
        {
            Path.Combine(AppContext.BaseDirectory, "..", "..", "backend", "serve.py"),
            Path.Combine(AppContext.BaseDirectory, "backend", "serve.py"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                "git", "groq-whisper", "backend", "serve.py"),
        };

        foreach (var path in candidates)
        {
            var full = Path.GetFullPath(path);
            if (File.Exists(full))
                return full;
        }

        return "serve.py";
    }
}
