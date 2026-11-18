namespace Sts2Mod.Core.Execution;

/// <summary>
/// Bounded frame barrier that allows map opening animations to finish before drawing map ink.
/// </summary>
public sealed class MapOpeningBarrier(bool ownershipKnown, int maxFrames = 600)
{
    private int _frames;

    public bool TimedOut { get; private set; }

    public bool Advance(bool animationIsLive)
    {
        if (!ownershipKnown)
            return false;
        if (!animationIsLive)
            return true;
        if (++_frames <= maxFrames)
            return false;
        TimedOut = true;
        return true;
    }
}
