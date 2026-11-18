namespace Sts2Mod.Core.Identity;

/// <summary>
/// Monotonic run-instance sequence scoped to the current Mod process lifetime.
/// </summary>
public sealed class RunInstanceSequencer
{
    private long _current;
    private bool _started;
    private bool _active;

    public long Current => _current;
    public bool HasStarted => _started;
    public bool IsActive => _active;

    public long BeginRun()
    {
        _current = checked(_current + 1);
        _started = true;
        _active = true;
        return _current;
    }

    public void MarkRunEnded() => _active = false;
}
