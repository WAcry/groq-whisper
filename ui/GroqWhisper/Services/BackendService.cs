using System.Diagnostics;
using System.Net;
using System.Net.Sockets;

namespace GroqWhisper.Services;

public sealed class BackendService
{
    private const int HealthCheckIntervalMs = 500;
    private const int HealthCheckTimeoutMs = 15_000;

    private Process? _process;
    private HttpClient? _http;

    public string BaseUrl { get; private set; } = "";
    public bool IsRunning => _process is { HasExited: false };
    public event Action<int>? BackendExited;

    public async Task LaunchAsync(string? pythonPath = null, string? servePath = null)
    {
        var python = pythonPath ?? FindPython();
        var serve = servePath ?? FindServePath();
        await VerifyPythonDependenciesAsync(python);
        var port = FindFreePort();
        BaseUrl = $"http://127.0.0.1:{port}";
        _http = new HttpClient { BaseAddress = new Uri(BaseUrl) };

        var startInfo = new ProcessStartInfo
        {
            FileName = python,
            Arguments = $"\"{serve}\" --host 127.0.0.1 --port {port}",
            WorkingDirectory = Path.GetDirectoryName(serve) ?? ".",
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };

        _process = Process.Start(startInfo);
        if (_process is null)
            throw new InvalidOperationException("Failed to start backend process.");

        _process.EnableRaisingEvents = true;
        _process.Exited += (_, _) => BackendExited?.Invoke(_process?.ExitCode ?? -1);
        _process.OutputDataReceived += (_, e) => Debug.WriteLine($"[backend] {e.Data}");
        _process.ErrorDataReceived += (_, e) => Debug.WriteLine($"[backend:err] {e.Data}");
        _process.BeginOutputReadLine();
        _process.BeginErrorReadLine();

        try
        {
            await WaitForReadyAsync(TimeSpan.FromMilliseconds(HealthCheckTimeoutMs));
        }
        catch
        {
            await ShutdownAsync();
            throw;
        }
    }

    public async Task WaitForReadyAsync(TimeSpan timeout)
    {
        if (_http is null) throw new InvalidOperationException("Backend not launched");
        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            if (_process is { HasExited: true })
                throw new InvalidOperationException($"Backend process exited with code {_process.ExitCode}");
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
            if (_http is not null)
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

    private static string FindPython()
    {
        var candidates = new[]
        {
            Path.Combine(AppContext.BaseDirectory, "python", "python.exe"),
            Path.Combine(AppContext.BaseDirectory, ".venv", "Scripts", "python.exe"),
            Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "..", "..", ".venv", "Scripts", "python.exe"),
            Path.Combine(Environment.CurrentDirectory, ".venv", "Scripts", "python.exe"),
        };

        foreach (var candidate in candidates)
        {
            var full = Path.GetFullPath(candidate);
            if (File.Exists(full))
                return full;
        }

        return "python";
    }

    private static async Task VerifyPythonDependenciesAsync(string python)
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = python,
                Arguments = "-c \"import fastapi, uvicorn, numpy\"",
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardError = true,
            };
            var proc = Process.Start(psi);
            if (proc is null)
                throw new InvalidOperationException($"Could not start Python at '{python}'");
            await proc.WaitForExitAsync();
            if (proc.ExitCode != 0)
            {
                var stderr = await proc.StandardError.ReadToEndAsync();
                throw new InvalidOperationException(
                    $"Python dependency check failed (exit {proc.ExitCode}): {stderr.Trim()}");
            }
        }
        catch (System.ComponentModel.Win32Exception ex)
        {
            throw new FileNotFoundException(
                $"Python not found at '{python}'. Install Python 3.11+ or place an embedded runtime in the 'python' directory.",
                ex);
        }
    }

    private static int FindFreePort()
    {
        using var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    private static string FindServePath()
    {
        var candidates = new[]
        {
            Path.Combine(AppContext.BaseDirectory, "backend", "serve.py"),
            Path.Combine(AppContext.BaseDirectory, "..", "..", "backend", "serve.py"),
            Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "backend", "serve.py"),
        };

        foreach (var path in candidates)
        {
            var full = Path.GetFullPath(path);
            if (File.Exists(full))
                return full;
        }

        throw new FileNotFoundException(
            $"Backend serve.py not found. Searched: {string.Join(", ", candidates.Select(Path.GetFullPath))}");
    }
}
