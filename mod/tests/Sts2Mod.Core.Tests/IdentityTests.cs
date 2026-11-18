using Sts2Mod.Core.Identity;
using Xunit;

namespace Sts2Mod.Core.Tests;

public sealed class IdentityTests
{
    private sealed class DummyCard;
    private sealed class DummyRelic;

    [Fact]
    public void NodeObjectRegistry_ManagesNodeEpochsRegistrationAndRetirement()
    {
        var registry = new NodeObjectRegistry();
        var nodeA = new object();
        var nodeB = new object();
        var card1 = new DummyCard();
        var card2 = new DummyCard();
        var relic = new DummyRelic();

        Assert.True(registry.EnterNode(nodeA));
        Assert.False(registry.EnterNode(nodeA));
        Assert.Equal("node-1", registry.NodeId);

        registry.Register(card1, PublicObjectKind.Card);
        registry.Register(card1, PublicObjectKind.Card);
        registry.Register(relic, PublicObjectKind.Relic);

        Assert.Equal("card:1:1", registry.GetRef(card1));
        Assert.Equal("relic:1:2", registry.GetRef(relic));

        Assert.True(registry.TryResolve<DummyCard>("card:1:1", out var resolvedCard));
        Assert.Same(card1, resolvedCard);
        Assert.False(registry.TryResolve<DummyRelic>("card:1:1", out _));

        registry.Retire(card1);
        Assert.False(registry.TryResolve<DummyCard>("card:1:1", out _));
        Assert.Throws<IdentityUnregisteredException>(() => registry.GetRef(card1));
        Assert.Throws<InvalidOperationException>(() => registry.Register(card1, PublicObjectKind.Card));
        Assert.Throws<IdentityUnregisteredException>(() => registry.GetRef(card2));

        Assert.True(registry.EnterNode(nodeB));
        Assert.Equal("node-2", registry.NodeId);
        Assert.False(registry.TryResolve<DummyRelic>("relic:1:2", out _));

        registry.Register(card1, PublicObjectKind.Card);
        Assert.Equal("card:2:1", registry.GetRef(card1));
    }

    [Fact]
    public void NodeObjectRegistry_RejectsOffThreadAccess()
    {
        var registry = new NodeObjectRegistry();
        registry.EnterNode(new object());
        var item = new DummyCard();
        registry.Register(item, PublicObjectKind.Card);

        Exception? captured = null;
        var worker = new Thread(() =>
        {
            try
            {
                _ = registry.GetRef(item);
            }
            catch (Exception error)
            {
                captured = error;
            }
        });
        worker.Start();
        worker.Join();

        Assert.IsType<InvalidOperationException>(captured);
    }

    [Fact]
    public void RunInstanceSequencer_AdvancesMonotonicallyAcrossRuns()
    {
        var sequencer = new RunInstanceSequencer();
        Assert.False(sequencer.HasStarted);
        Assert.False(sequencer.IsActive);

        Assert.Equal(0, sequencer.Current);
        Assert.Equal(1, sequencer.BeginRun());
        Assert.True(sequencer.HasStarted);
        Assert.True(sequencer.IsActive);
        Assert.Equal(1, sequencer.Current);

        sequencer.MarkRunEnded();
        Assert.False(sequencer.IsActive);
        Assert.Equal(1, sequencer.Current);
        Assert.Equal(2, sequencer.BeginRun());
        Assert.True(sequencer.IsActive);
    }
}
