using System.Globalization;

namespace Sts2Mod.Core.Developer;

/// <summary>
/// Instant presentation configuration for explicit headless replay launches.
/// </summary>
public static class ReplayPresentation
{
    public static bool Parse(string? value, bool headless, bool replay)
    {
        if (value is null)
            return false;
        if (value != "instant" || !headless || !replay)
            throw new InvalidOperationException("Instant replay presentation is supported only for explicit headless replay launches.");
        return true;
    }
}

public sealed class ReplayPresentationLifecycle(bool enabled)
{
    private Action? _restore;

    public bool Enabled { get; } = enabled;
    public bool Active { get; private set; }
    public bool Restored { get; private set; }

    public bool Activate(Action activate, Action restore)
    {
        if (!Enabled || Active || Restored)
            return false;
        activate();
        _restore = restore;
        Active = true;
        return true;
    }

    public bool Restore()
    {
        if (Restored)
            return true;
        if (!Active)
            return false;
        _restore!();
        Active = false;
        Restored = true;
        _restore = null;
        return true;
    }
}

/// <summary>
/// Base time-scale multiplier for explicit headless replay launches, preserving relative HitStop scaling.
/// </summary>
public sealed record ReplayTimeScale(double BaseScale)
{
    public static ReplayTimeScale? Parse(string? value, bool headless, bool replay)
    {
        if (value is null)
            return null;
        if (!headless || !replay)
            throw new InvalidOperationException("Replay base time scale is supported only for explicit headless replay launches.");
        if (!double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var scale)
            || !double.IsFinite(scale)
            || scale is < 1 or > 4)
        {
            throw new ArgumentOutOfRangeException(nameof(value), "Replay base time scale must be between 1 and 4.");
        }
        return new ReplayTimeScale(scale);
    }

    public float ApplyHitStop(float relativeScale) => (float)(BaseScale * relativeScale);
}
