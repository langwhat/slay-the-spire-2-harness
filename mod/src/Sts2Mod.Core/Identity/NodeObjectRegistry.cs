namespace Sts2Mod.Core.Identity;

public enum PublicObjectKind
{
    Card,
    Potion,
    Relic,
    Enemy,
    Ally,
    Selection,
    Offer,
    Event,
    Option,
    Bundle,
    Interaction,
}

public sealed class IdentityUnregisteredException()
    : InvalidOperationException("A native object was not registered before publication.");

/// <summary>
/// Owns node-scoped public references on the game thread.
/// Publication and inspection read only installed references without allocating new identities.
/// </summary>
public sealed class NodeObjectRegistry
{
    private sealed record Entry(string Ref, PublicObjectKind Kind)
    {
        public bool Retired { get; set; }
    }

    private readonly Dictionary<object, Entry> _objects = new(ReferenceEqualityComparer.Instance);
    private readonly Dictionary<string, object> _refs = new(StringComparer.Ordinal);
    private readonly int _thread = Environment.CurrentManagedThreadId;
    private object? _node;
    private long _epoch;
    private long _next;

    public string NodeId => $"node-{_epoch}";

    public bool EnterNode(object node)
    {
        AssertThread();
        if (ReferenceEquals(node, _node))
            return false;
        _node = node;
        _epoch++;
        _next = 0;
        _objects.Clear();
        _refs.Clear();
        return true;
    }

    public void Register(object instance, PublicObjectKind kind)
    {
        AssertThread();
        if (_objects.TryGetValue(instance, out var existing))
        {
            if (existing.Retired)
                throw new InvalidOperationException("A retired native object cannot re-enter this node.");
            return;
        }
        var reference = $"{kind.ToString().ToLowerInvariant()}:{_epoch}:{++_next}";
        _objects.Add(instance, new Entry(reference, kind));
        _refs.Add(reference, instance);
    }

    public string GetRef(object instance)
    {
        AssertThread();
        return _objects.TryGetValue(instance, out var entry) && !entry.Retired
            ? entry.Ref
            : throw new IdentityUnregisteredException();
    }

    public bool TryResolve<T>(string reference, out T? instance) where T : class
    {
        AssertThread();
        instance = _refs.TryGetValue(reference, out var value) ? value as T : null;
        return instance is not null;
    }

    public void Retire(object instance)
    {
        AssertThread();
        if (!_objects.TryGetValue(instance, out var entry))
            throw new IdentityUnregisteredException();
        entry.Retired = true;
        _refs.Remove(entry.Ref);
    }

    private void AssertThread()
    {
        if (Environment.CurrentManagedThreadId != _thread)
            throw new InvalidOperationException("Native object identities may only be accessed on the game thread.");
    }
}
