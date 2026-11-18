using Sts2Mod.Core.Transport;

namespace Sts2Mod.Core.Developer;

/// <summary>
/// Immutable game-neutral snapshot of main-menu readiness captured on the game thread.
/// </summary>
public readonly record struct MainMenuReadinessObservation(
    bool GamePresent,
    bool MainMenuPresent,
    bool MainMenuValid,
    bool MainMenuInsideTree,
    bool MainMenuVisibleInTree,
    bool SubmenuStackPresent,
    bool SubmenusOpen,
    bool TransitionInProgress);

public static class MainMenuReadinessRules
{
    public static bool IsRunStartReady(MainMenuReadinessObservation observation) =>
        observation.MainMenuPresent
        && observation.MainMenuValid
        && observation.MainMenuInsideTree
        && observation.MainMenuVisibleInTree
        && observation.SubmenuStackPresent
        && !observation.SubmenusOpen;

    public static bool IsMenuSettled(MainMenuReadinessObservation observation) =>
        observation.GamePresent
        && !observation.TransitionInProgress
        && IsRunStartReady(observation);
}

/// <summary>
/// One-shot headless run recipe read from <c>STS2AGENT_START_RUN_FILE</c>.
/// </summary>
public sealed record HeadlessRunConfiguration(string Character, int Ascension = 0, string? Seed = null)
{
    public static HeadlessRunConfiguration Parse(string json)
    {
        var value = JsonContract.DeserializeUnique<HeadlessRunConfiguration>(json)
                    ?? throw new ArgumentException("Headless run configuration cannot be empty.");
        if (value.Character is not ("IRONCLAD" or "SILENT" or "DEFECT" or "REGENT" or "NECROBINDER")
            || value.Ascension is < 0 or > 10
            || value.Seed is { Length: 0 })
        {
            throw new ArgumentException("Invalid headless run character, ascension level, or seed.");
        }
        return value;
    }
}

/// <summary>
/// Verified start receipt written to <c>&lt;start_file&gt;.result.json</c> for <c>sts2_harness.headless</c>.
/// </summary>
public sealed record HeadlessStartReceipt(
    string Status,
    bool EffectVerified,
    string? ReasonCode,
    string Seed)
{
    public static HeadlessStartReceipt Completed(string seed) =>
        new("completed", true, null, seed);
}
