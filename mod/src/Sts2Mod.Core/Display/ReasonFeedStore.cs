using System.Collections.Immutable;
using System.Text;
using System.Text.Json;
using Sts2Mod.Core.Transport;

namespace Sts2Mod.Core.Display;

public enum DisplayReplyStatus
{
    Shown,
}

public sealed record DisplayReply(DisplayReplyStatus Status);

public sealed record ReasonFeedEntry(long Id, string Text, DateTimeOffset RecordedAt);

/// <summary>
/// Thread-safe bounded store of recent decision reasons for the in-game viewer overlay.
/// </summary>
public sealed class ReasonFeedStore
{
    public const int DefaultMaxEntries = 4;
    public const int DefaultMaxReasonLength = 240;
    public static readonly TimeSpan DefaultEntryLifetime = TimeSpan.FromSeconds(22);

    private readonly object _sync = new();
    private readonly List<ReasonFeedEntry> _entries = [];
    private readonly int _maxEntries;
    private readonly int _maxReasonLength;
    private readonly TimeSpan _entryLifetime;
    private long _nextId;
    private long _version;

    public ReasonFeedStore(
        int maxEntries = DefaultMaxEntries,
        TimeSpan? entryLifetime = null,
        int maxReasonLength = DefaultMaxReasonLength)
    {
        if (maxEntries <= 0)
            throw new ArgumentOutOfRangeException(nameof(maxEntries));
        if (maxReasonLength < 16)
            throw new ArgumentOutOfRangeException(nameof(maxReasonLength));

        var lifetime = entryLifetime ?? DefaultEntryLifetime;
        if (lifetime <= TimeSpan.Zero)
            throw new ArgumentOutOfRangeException(nameof(entryLifetime));

        _maxEntries = maxEntries;
        _maxReasonLength = maxReasonLength;
        _entryLifetime = lifetime;
    }

    public long Version => Volatile.Read(ref _version);

    public TimeSpan EntryLifetime => _entryLifetime;

    public void Record(string reason, DateTimeOffset now)
    {
        var normalized = NormalizeReason(reason, _maxReasonLength);
        if (normalized.Length == 0)
            throw new ArgumentException("Reason must contain non-whitespace text.", nameof(reason));

        RecordNormalized(normalized, now);
    }

    private void RecordNormalized(string normalized, DateTimeOffset now)
    {
        lock (_sync)
        {
            PruneExpiredLocked(now);
            if (_entries.Count > 0 && string.Equals(_entries[0].Text, normalized, StringComparison.Ordinal))
            {
                _entries[0] = _entries[0] with { RecordedAt = now };
                Interlocked.Increment(ref _version);
                return;
            }

            _entries.Insert(0, new ReasonFeedEntry(++_nextId, normalized, now));
            if (_entries.Count > _maxEntries)
                _entries.RemoveRange(_maxEntries, _entries.Count - _maxEntries);
            Interlocked.Increment(ref _version);
        }
    }

    public ImmutableArray<ReasonFeedEntry> GetActive(DateTimeOffset now, int maxCount = 3)
    {
        if (maxCount <= 0)
            return [];

        lock (_sync)
        {
            if (PruneExpiredLocked(now))
                Interlocked.Increment(ref _version);

            var count = Math.Min(maxCount, _entries.Count);
            if (count == 0)
                return [];

            var builder = ImmutableArray.CreateBuilder<ReasonFeedEntry>(count);
            for (var index = 0; index < count; index++)
                builder.Add(_entries[index]);
            return builder.MoveToImmutable();
        }
    }

    public void Clear()
    {
        lock (_sync)
        {
            if (_entries.Count == 0)
                return;
            _entries.Clear();
            Interlocked.Increment(ref _version);
        }
    }

    public HttpReply WriteJson(string body, DateTimeOffset? now = null)
    {
        try
        {
            JsonContract.EnsureNoDuplicateProperties(body);
            using var document = JsonDocument.Parse(body);
            var root = document.RootElement;
            JsonContract.RequireKeys(root, ["reason"]);
            var rawReason = JsonContract.RequireText(root, "reason");
            var normalized = NormalizeReason(rawReason, _maxReasonLength);
            if (normalized.Length == 0)
                throw new JsonException("Reason must contain non-whitespace text.");

            RecordNormalized(normalized, now ?? DateTimeOffset.UtcNow);
            return new HttpReply(200, JsonContract.Serialize(new DisplayReply(DisplayReplyStatus.Shown)));
        }
        catch (JsonException)
        {
            return new HttpReply(
                400,
                InvalidRequestReply.Json("The request does not match the display contract.", "reason"));
        }
    }

    private bool PruneExpiredLocked(DateTimeOffset now) =>
        _entries.RemoveAll(entry => now - entry.RecordedAt >= _entryLifetime) > 0;

    public static string NormalizeReason(string raw, int maxLength = DefaultMaxReasonLength)
    {
        if (string.IsNullOrWhiteSpace(raw))
            return string.Empty;

        var builder = new StringBuilder(Math.Min(raw.Length, maxLength + 1));
        var previousWasSpace = true;
        foreach (var ch in raw)
        {
            if (char.IsWhiteSpace(ch))
            {
                if (!previousWasSpace)
                {
                    builder.Append(' ');
                    previousWasSpace = true;
                }
                continue;
            }

            builder.Append(ch);
            previousWasSpace = false;
        }

        if (builder.Length > 0 && builder[^1] == ' ')
            builder.Length--;

        if (builder.Length <= maxLength)
            return builder.ToString();

        var cutoff = maxLength - 1;
        while (cutoff > 0 && char.IsHighSurrogate(builder[cutoff - 1]))
            cutoff--;
        while (cutoff > 0 && builder[cutoff - 1] == ' ')
            cutoff--;
        return string.Concat(builder.ToString(0, cutoff), "…");
    }
}
