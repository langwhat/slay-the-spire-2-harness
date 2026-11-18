using System.Collections.Immutable;
using System.Text.Json.Serialization;
using Sts2Mod.Core.Observation;

namespace Sts2Mod.Core.Developer;

public sealed record LaboratoryCardState(
    string DefinitionId,
    bool Upgraded,
    string? EnchantmentId);

public sealed record LaboratoryPlayerState(
    int PlayerCount,
    int Hp,
    int MaxHp,
    int PotionSlotCount,
    bool IsMapSelectionReady,
    string? EncounterId,
    ObservationPhase Phase,
    ImmutableArray<LaboratoryCardState> Deck,
    ImmutableArray<string> Relics,
    ImmutableArray<string> Potions);

public sealed record CombatEvalStep(string Command, Func<LaboratoryPlayerState, bool> IsComplete);

public enum LaboratoryOutcome
{
    Completed,
    Rejected,
    Unknown,
}

public enum LaboratoryErrorCode
{
    InvalidRecipe,
    Unsupported,
    NativeBusy,
    DevelopmentDisabled,
    NativeConflict,
    NativeIncomplete,
}

public sealed record LaboratoryError(LaboratoryErrorCode Code, string Message);

public sealed record LaboratoryReply(
    LaboratoryOutcome Status,
    LaboratoryError? Error,
    NativeObservation? PreparedObservation,
    NativeObservation? Observation);

/// <summary>
/// Developer-only combat evaluation scenario constructed from a fresh single-player map state.
/// </summary>
public sealed record CombatEvalScenario(
    [property: JsonRequired] string[] Deck,
    [property: JsonRequired] string[] Relics,
    [property: JsonRequired] string[] Potions,
    [property: JsonRequired] string Encounter)
{
    public void Validate()
    {
        if (Deck is null
            || Relics is null
            || Potions is null
            || Deck.Length == 0
            || Relics.Distinct(StringComparer.Ordinal).Count() != Relics.Length
            || !Deck.All(id => IsDefinitionId(id, "CARD."))
            || !Relics.All(id => IsDefinitionId(id, "RELIC."))
            || !Potions.All(id => IsDefinitionId(id, "POTION."))
            || !IsDefinitionId(Encounter, "ENCOUNTER."))
        {
            throw new ArgumentException("Use full native definition IDs, a non-empty deck, and distinct relics.");
        }
    }

    public IReadOnlyList<CombatEvalStep> Prepare(LaboratoryPlayerState state)
    {
        Validate();
        if (state.PlayerCount != 1
            || state.Phase != ObservationPhase.Run
            || !state.IsMapSelectionReady)
        {
            throw new ArgumentException("Laboratory setup requires a ready single-player map.");
        }

        if (state.Potions.Length != 0 || Potions.Length > state.PotionSlotCount)
            throw new ArgumentException("Start with empty potion slots and request no more than the available slots.");

        var steps = new List<CombatEvalStep>();
        foreach (var group in state.Deck.GroupBy(card => card.DefinitionId, StringComparer.Ordinal))
        {
            for (var remaining = group.Count() - 1; remaining >= 0; remaining--)
            {
                var count = remaining;
                var id = group.Key;
                steps.Add(new($"remove_card {EntryName(id)} Deck", current =>
                    current.Deck.Count(card => card.DefinitionId == id) == count));
            }
        }

        foreach (var id in state.Relics)
        {
            var relicId = id;
            steps.Add(new($"relic remove {EntryName(relicId)}", current =>
                current.Relics.All(relic => relic != relicId)));
        }

        foreach (var group in Deck.GroupBy(id => id, StringComparer.Ordinal))
        {
            for (var added = 1; added <= group.Count(); added++)
            {
                var count = added;
                var id = group.Key;
                steps.Add(new($"card {EntryName(id)} Deck", current =>
                    current.Deck.Count(card => card.DefinitionId == id) >= count));
            }
        }

        foreach (var id in Relics)
        {
            var relicId = id;
            steps.Add(new($"relic add {EntryName(relicId)}", current =>
                current.Relics.Any(relic => relic == relicId)));
        }

        foreach (var group in Potions.GroupBy(id => id, StringComparer.Ordinal))
        {
            for (var added = 1; added <= group.Count(); added++)
            {
                var count = added;
                var id = group.Key;
                steps.Add(new($"potion {EntryName(id)}", current =>
                    current.Potions.Count(potion => potion == id) >= count));
            }
        }

        return steps;
    }

    public CombatEvalStep FillHealth(LaboratoryPlayerState state) =>
        new($"heal {state.MaxHp}", current => current.Hp == current.MaxHp);

    public CombatEvalStep Fight() =>
        new($"fight {EntryName(Encounter)}", current =>
            current.Phase == ObservationPhase.Combat && current.EncounterId == Encounter);

    public bool MatchesResources(LaboratoryPlayerState state) =>
        Deck.GroupBy(id => id, StringComparer.Ordinal).All(group =>
            state.Deck.Count(card =>
                card.DefinitionId == group.Key
                && !card.Upgraded
                && card.EnchantmentId is null) >= group.Count())
        && Relics.All(id => state.Relics.Any(relic => relic == id))
        && Potions.GroupBy(id => id, StringComparer.Ordinal).All(group =>
            state.Potions.Count(potion => potion == group.Key) >= group.Count());

    private static string EntryName(string modelId) => modelId[(modelId.IndexOf('.') + 1)..];

    private static bool IsDefinitionId(string? id, string prefix) =>
        id is not null
        && id.StartsWith(prefix, StringComparison.Ordinal)
        && id.Length > prefix.Length
        && id[prefix.Length..].All(c => c is (>= 'A' and <= 'Z') or (>= '0' and <= '9') or '_');
}
