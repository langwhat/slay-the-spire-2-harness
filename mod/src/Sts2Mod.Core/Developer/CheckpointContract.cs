using System.Collections.Immutable;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Sts2Mod.Core.Execution;
using Sts2Mod.Core.Observation;
using Sts2Mod.Core.Transport;

namespace Sts2Mod.Core.Developer;

public sealed record ReloadTarget(
    string CanonicalSeed,
    string GameMode,
    string Character,
    int Ascension,
    int Act,
    int Floor);

public enum CheckpointKind
{
    Capture,
    Load,
    Status,
}

public enum CheckpointOutcome
{
    Pending,
    Completed,
    Rejected,
    Unknown,
}

public enum CheckpointErrorCode
{
    Disabled,
    InvalidRequest,
    NotFound,
    Conflict,
    Busy,
    NativeRejected,
    ExecutionUncertain,
}

public abstract record CheckpointRequest(string CommandId, CheckpointKind Kind);

public sealed record CaptureCheckpointRequest(
    string CommandId,
    StepRoute ExpectedRoute,
    string OutputDirectory) : CheckpointRequest(CommandId, CheckpointKind.Capture);

public sealed record LoadCheckpointRequest(
    string CommandId,
    string CheckpointPath,
    string CheckpointSha256,
    ReloadTarget Target) : CheckpointRequest(CommandId, CheckpointKind.Load);

public sealed record CheckpointStatusRequest(string CommandId)
    : CheckpointRequest(CommandId, CheckpointKind.Status);

public sealed record CheckpointFileProof(string Path, string Sha256, long Bytes);

public sealed record CheckpointStateProof(
    StepRoute Route,
    long NativeRunInstanceId,
    long NativeRevision,
    ReloadTarget Target);

[JsonDerivedType(typeof(CapturedCheckpointProof))]
[JsonDerivedType(typeof(LoadedCheckpointProof))]
public abstract record CheckpointProof(
    CheckpointKind Kind,
    CheckpointStateProof State,
    CheckpointFileProof File);

public sealed record CapturedCheckpointProof(
    CheckpointStateProof State,
    CheckpointFileProof File) : CheckpointProof(CheckpointKind.Capture, State, File);

public sealed record LoadedCheckpointProof(
    CheckpointStateProof State,
    CheckpointFileProof File) : CheckpointProof(CheckpointKind.Load, State, File);

public sealed record CheckpointCause(
    CheckpointErrorCode Code,
    string Message,
    string? NativeReason = null);

[JsonDerivedType(typeof(PendingCheckpoint))]
[JsonDerivedType(typeof(CompletedCheckpoint))]
[JsonDerivedType(typeof(RejectedCheckpoint))]
[JsonDerivedType(typeof(UnknownCheckpoint))]
public abstract record CheckpointReply(string? CommandId, CheckpointOutcome Status);

public sealed record PendingCheckpoint(string CommandId, bool NativeDispatched)
    : CheckpointReply(CommandId, CheckpointOutcome.Pending);

public sealed record CompletedCheckpoint(string CommandId, CheckpointProof Proof)
    : CheckpointReply(CommandId, CheckpointOutcome.Completed);

public sealed record RejectedCheckpoint(string? CommandId, CheckpointCause Error)
    : CheckpointReply(CommandId, CheckpointOutcome.Rejected);

public sealed record UnknownCheckpoint(string CommandId, bool NativeDispatched, CheckpointCause Error)
    : CheckpointReply(CommandId, CheckpointOutcome.Unknown);

public static class CheckpointContract
{
    public const int MaxCommandIdLength = 128;

    public static bool IsValidCommandId(string? commandId)
    {
        if (string.IsNullOrEmpty(commandId) || commandId.Length > MaxCommandIdLength)
            return false;

        foreach (var ch in commandId)
        {
            var allowed = ch is (>= 'a' and <= 'z')
                or (>= 'A' and <= 'Z')
                or (>= '0' and <= '9')
                or '-'
                or '_'
                or '.'
                or ':';
            if (!allowed)
                return false;
        }

        return true;
    }

    public static CheckpointRequest Decode(string body)
    {
        JsonContract.EnsureNoDuplicateProperties(body);
        using var document = JsonDocument.Parse(body);
        var root = document.RootElement;
        if (root.ValueKind != JsonValueKind.Object || !root.TryGetProperty("kind", out var kind))
            throw new JsonException();
        if (kind.ValueKind != JsonValueKind.String)
            throw new JsonException();

        CheckpointRequest request = kind.GetString() switch
        {
            "capture" => JsonSerializer.Deserialize<CaptureCheckpointRequest>(body, JsonContract.Options)!,
            "load" => JsonSerializer.Deserialize<LoadCheckpointRequest>(body, JsonContract.Options)!,
            "status" => JsonSerializer.Deserialize<CheckpointStatusRequest>(body, JsonContract.Options)!,
            _ => throw new JsonException(),
        };

        if (request is null || !IsValidCommandId(request.CommandId))
            throw new JsonException();

        if ((request is CaptureCheckpointRequest
             && (!root.TryGetProperty("expected_route", out var routeJson)
                 || routeJson.ValueKind != JsonValueKind.Object
                 || routeJson.EnumerateObject().Count() != 4))
            || (request is LoadCheckpointRequest
                && (!root.TryGetProperty("target", out var targetJson)
                    || targetJson.ValueKind != JsonValueKind.Object
                    || targetJson.EnumerateObject().Count() != 6)))
        {
            throw new JsonException();
        }

        switch (request)
        {
            case CaptureCheckpointRequest capture when capture.ExpectedRoute is not { } route
                || string.IsNullOrWhiteSpace(route.GameInstanceId)
                || string.IsNullOrWhiteSpace(route.RunId)
                || string.IsNullOrWhiteSpace(route.NodeId)
                || !Enum.IsDefined(route.Owner)
                || string.IsNullOrWhiteSpace(capture.OutputDirectory)
                || !Path.IsPathFullyQualified(capture.OutputDirectory):
            case LoadCheckpointRequest load when string.IsNullOrWhiteSpace(load.CheckpointPath)
                || !Path.IsPathFullyQualified(load.CheckpointPath)
                || Path.GetFileName(load.CheckpointPath) != "current_run.save"
                || load.CheckpointSha256 is not { Length: 64 } hash
                || hash.Any(c => c is not ((>= '0' and <= '9') or (>= 'a' and <= 'f')))
                || load.Target is not { } target
                || string.IsNullOrWhiteSpace(target.CanonicalSeed)
                || string.IsNullOrWhiteSpace(target.GameMode)
                || string.IsNullOrWhiteSpace(target.Character)
                || target.Ascension < 0
                || target.Act < 1
                || target.Floor < 0:
                throw new JsonException();
        }

        return request;
    }

    public static string Fingerprint(CheckpointRequest request) =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(
            JsonSerializer.Serialize(request, request.GetType(), JsonContract.Options)))).ToLowerInvariant();
}

public sealed record CheckpointDeckCardIdentity(ulong OwnerNetId, uint DeckIndex);

public sealed record CheckpointCardIdentity(
    string Ref,
    uint? CombatCardId,
    CheckpointDeckCardIdentity? DeckCard);

public sealed record CheckpointCreatureIdentity(string Ref, uint? CombatId);

public sealed record CheckpointMapIdentity(string Ref, int Act, int Row, int Col);

public sealed record CheckpointRelicIdentity(string Ref, ulong OwnerNetId, int Slot);

public enum CheckpointRewardBindingKind
{
    RewardObject,
    ClaimAction,
    ChoiceScreen,
    CreationCard,
    PickAction,
    AlternativeAction,
    ProceedAction,
}

public sealed record CheckpointRewardAddress(
    ulong OwnerNetId,
    int? NativeRewardsSetIndex,
    CheckpointRewardBindingKind BindingKind,
    int? CreationSlot,
    string? NativeOptionId);

public sealed record CheckpointRewardIdentity(string Ref, CheckpointRewardAddress Binding);

public sealed record CheckpointIdentityFacts(
    ImmutableArray<CheckpointCardIdentity> Cards,
    ImmutableArray<CheckpointCreatureIdentity> Creatures,
    ImmutableArray<CheckpointMapIdentity> MapTravel,
    ImmutableArray<CheckpointRelicIdentity> Relics,
    ImmutableArray<CheckpointRewardIdentity> RewardBindings);

[JsonDerivedType(typeof(CompletedCheckpointIdentityRead))]
[JsonDerivedType(typeof(FailedCheckpointIdentityRead))]
public abstract record CheckpointIdentityRead(ReadOutcome Status);

public sealed record CompletedCheckpointIdentityRead(
    NativeObservation Observation,
    CheckpointIdentityFacts Identities) : CheckpointIdentityRead(ReadOutcome.Completed);

public sealed record FailedCheckpointIdentityRead(ReadCause Error)
    : CheckpointIdentityRead(ReadOutcome.Failed);

public static class CheckpointIdentityReader
{
    public static CheckpointIdentityRead Read(NativeReadPublication publication) =>
        publication.Reply switch
        {
            FailedStateRead failed => new FailedCheckpointIdentityRead(failed.Error),
            CompletedStateRead completed when publication.CheckpointIdentities is { } identities
                                              && completed.Observation.State is not TerminalStateView =>
                new CompletedCheckpointIdentityRead(completed.Observation, identities),
            _ => new FailedCheckpointIdentityRead(new(
                NativeReadError.ReadUnavailable,
                "Native identity evidence is unavailable for this publication.")),
        };

    public static string ReadJson(NativeReadPublication publication)
    {
        var read = Read(publication);
        var json = JsonContract.Serialize(read);
        return read is CompletedCheckpointIdentityRead completed
            ? SemanticText.Json(json, completed.Observation.GameLanguage)
            : json;
    }
}

/// <summary>
/// Bounded in-memory receipt store for asynchronous <c>/operator/checkpoint</c> commands.
/// </summary>
public sealed class CheckpointCommandStore(int capacity = 256)
{
    public sealed record Entry(
        string CommandId,
        CheckpointKind Kind,
        string Fingerprint,
        CheckpointReply Reply,
        bool IsTerminal);

    private readonly object _sync = new();
    private readonly Dictionary<string, Entry> _entries = new(StringComparer.Ordinal);
    private readonly LinkedList<string> _order = new();

    public Entry? Get(string commandId)
    {
        lock (_sync)
            return _entries.GetValueOrDefault(commandId);
    }

    public bool HasNonTerminalOtherThan(string? commandId = null)
    {
        lock (_sync)
            return _entries.Values.Any(entry => !entry.IsTerminal && entry.CommandId != commandId);
    }

    public (Entry Entry, bool Added) ReservePending(string commandId, CheckpointKind kind, string fingerprint)
    {
        lock (_sync)
        {
            if (_entries.TryGetValue(commandId, out var existing))
                return (existing, false);

            EvictIfNeededLocked();
            var entry = new Entry(
                commandId,
                kind,
                fingerprint,
                new PendingCheckpoint(commandId, NativeDispatched: false),
                IsTerminal: false);
            _entries[commandId] = entry;
            _order.AddLast(commandId);
            return (entry, true);
        }
    }

    public void MarkDispatched(string commandId)
    {
        lock (_sync)
        {
            if (!_entries.TryGetValue(commandId, out var current) || current.IsTerminal)
                return;
            _entries[commandId] = current with
            {
                Reply = new PendingCheckpoint(commandId, NativeDispatched: true),
            };
        }
    }

    public void Settle(string commandId, CheckpointReply reply)
    {
        lock (_sync)
        {
            if (!_entries.TryGetValue(commandId, out var current) || current.IsTerminal)
                return;
            _entries[commandId] = current with
            {
                Reply = reply,
                IsTerminal = true,
            };
        }
    }

    private void EvictIfNeededLocked()
    {
        while (_entries.Count >= capacity && _order.First is not null)
        {
            var node = _order.First;
            while (node is not null && _entries.TryGetValue(node.Value, out var candidate) && !candidate.IsTerminal)
                node = node.Next;
            if (node is null)
                break;
            _entries.Remove(node.Value);
            _order.Remove(node);
        }
    }
}
