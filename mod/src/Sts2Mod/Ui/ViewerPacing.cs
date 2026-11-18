using Godot;
using Sts2Mod.Developer;

namespace Sts2Mod.Ui;

/// <summary>
/// Distinguishes interactive GUI viewer mode from headless or fast-forward developer runs.
/// Headless and fast-forward runs always execute with zero presentation latency and no overlay work.
/// </summary>
internal static class ViewerPacing
{
    private static readonly Lazy<bool> CachedIsHeadless = new(static () => DisplayServer.GetName() == "headless");

    public static bool IsHeadless => CachedIsHeadless.Value;

    public static bool IsInteractiveGui => !IsHeadless && !ReplayPresentationPatch.Active;
}
