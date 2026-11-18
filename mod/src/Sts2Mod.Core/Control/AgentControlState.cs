using System.Text.Json;
using Sts2Mod.Core.Transport;

namespace Sts2Mod.Core.Control;

/// <summary>
/// Shared start/pause intent owned jointly by the in-game control overlay and the HTTP control endpoint.
/// </summary>
public enum AgentControlMode
{
    Paused,
    Running,
}

public enum ControlErrorCode
{
    ControlUnavailable,
}

public sealed record ControlReply(AgentControlMode Mode);

public sealed record ControlFailure(ControlErrorCode Code, string Message);

public sealed record ControlFailed(ControlFailure Error)
{
    public TechnicalFailureOutcome Status => TechnicalFailureOutcome.Failed;
}

public sealed class AgentControlState
{
    private int _mode = (int)AgentControlMode.Paused;
    private int _preserveModeForRunLoad;

    public event Action? Paused;

    public AgentControlMode Mode => (AgentControlMode)Volatile.Read(ref _mode);

    public bool IsRunning => Mode == AgentControlMode.Running;

    public AgentControlMode SetMode(AgentControlMode mode)
    {
        if (mode is not (AgentControlMode.Paused or AgentControlMode.Running))
            throw new ArgumentOutOfRangeException(nameof(mode));
        Volatile.Write(ref _mode, (int)mode);
        if (mode == AgentControlMode.Paused)
            Paused?.Invoke();
        return mode;
    }

    public AgentControlMode Toggle()
    {
        while (true)
        {
            var observed = Volatile.Read(ref _mode);
            var next = observed == (int)AgentControlMode.Running
                ? (int)AgentControlMode.Paused
                : (int)AgentControlMode.Running;
            if (Interlocked.CompareExchange(ref _mode, next, observed) == observed)
            {
                if (next == (int)AgentControlMode.Paused)
                    Paused?.Invoke();
                return (AgentControlMode)next;
            }
        }
    }

    public void ArmRunLoadBoundary() =>
        Volatile.Write(ref _preserveModeForRunLoad, 1);

    public void CancelRunLoadBoundary() =>
        Volatile.Write(ref _preserveModeForRunLoad, 0);

    public bool ApplyRunStartedBoundary()
    {
        var preserveMode = Interlocked.Exchange(ref _preserveModeForRunLoad, 0) != 0;
        if (!preserveMode)
            SetMode(AgentControlMode.Paused);
        return preserveMode;
    }

    public HttpReply WriteJson(string body)
    {
        try
        {
            JsonContract.EnsureNoDuplicateProperties(body);
            using var document = JsonDocument.Parse(body);
            var root = document.RootElement;
            JsonContract.RequireKeys(root, ["mode"]);
            var rawMode = JsonContract.RequireText(root, "mode");
            var mode = rawMode switch
            {
                "running" => AgentControlMode.Running,
                "paused" => AgentControlMode.Paused,
                _ => throw new JsonException("Unsupported control mode."),
            };
            return new HttpReply(200, JsonContract.Serialize(new ControlReply(SetMode(mode))));
        }
        catch (JsonException)
        {
            return new HttpReply(400, InvalidRequestReply.Json("The request does not match the control contract.", "mode"));
        }
    }
}
