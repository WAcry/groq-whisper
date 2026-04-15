namespace GroqWhisper.Models;

public enum ServiceState
{
    Idle,
    Preflight,
    Running,
    Paused,
    Error,
    Unknown
}

public static class ServiceStateExtensions
{
    public static ServiceState Parse(string? value) => value switch
    {
        "idle" => ServiceState.Idle,
        "preflight" => ServiceState.Preflight,
        "running" => ServiceState.Running,
        "paused" => ServiceState.Paused,
        "error" => ServiceState.Error,
        _ => ServiceState.Unknown,
    };
}
