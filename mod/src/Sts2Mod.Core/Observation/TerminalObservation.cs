using System.Collections.Immutable;
using System.Text.Json.Serialization;

namespace Sts2Mod.Core.Observation;

public enum RunResult
{
    Victory,
    Defeat,
    Escaped,
    Unknown,
}

public enum TerminalScreen
{
    RunComplete,
}

public sealed record TerminalStateView(
    PublicProgress Progress,
    IObservedPlayer Player,
    ImmutableArray<string> Relics,
    ImmutableArray<string> Potions,
    RunResult Result,
    ImmutableArray<string> Deck) : IObservedState
{
    public PublicInputControl Control => PublicInputControl.Unavailable;
    public ImmutableArray<PublicInteraction> Actions => [];
    public SelectionView? Selection => null;
    public ObservationPhase Phase => ObservationPhase.Terminal;
    public TerminalScreen Screen => TerminalScreen.RunComplete;

    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public int? LastObservedTurn { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public ImmutableArray<EnemyView>? Enemies { get; init; }
}

public static class TerminalObservation
{
    public static NativeReadPublication Create(NativeReadPublication previous, RunResult result, ObservationStamp stamp)
    {
        if (previous.Reply is not CompletedStateRead completed)
            return previous;

        var observation = completed.Observation;
        TerminalStateView state = observation.State switch
        {
            CombatStateView combat => new(
                combat.Progress,
                combat.Player,
                combat.Relics,
                combat.Potions,
                result,
                previous.Deck.IsDefault ? combat.Deck : previous.Deck)
            {
                LastObservedTurn = combat.Turn,
                Enemies = combat.Enemies.AddRange(combat.InactiveEnemies),
            },
            RunStateView run => new(
                run.Progress,
                run.Player,
                run.Relics,
                run.Potions,
                result,
                run.Deck),
            TerminalStateView => throw new InvalidOperationException("A terminal publication cannot end again."),
            _ => throw new InvalidOperationException("Unsupported observed state."),
        };

        return previous with
        {
            CheckpointIdentities = null,
            Reply = new CompletedStateRead(observation with
            {
                Stamp = stamp,
                Location = observation.Location with { Owner = null },
                InputReadiness = NativeInputReadiness.Terminal,
                State = state,
                Encounter = null,
            }),
        };
    }
}
