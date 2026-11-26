using System.Reflection;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Events;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Events;
using MegaCrit.Sts2.Core.Rewards;
using Sts2Mod.Core.Observation;
using Sts2Mod.Observation;
using Xunit;

namespace Sts2Mod.Native.Tests;

public sealed class HighFrequencyExtractorTests
{
    private const BindingFlags NonPublicInstance = BindingFlags.Instance | BindingFlags.NonPublic;

    public HighFrequencyExtractorTests()
    {
        NativeTestBootstrap.EnsureModelDb();
    }

    private static EventOption MakeOption(EventModel ev, Func<Task> onChosen)
        => new(ev, onChosen, "ABYSSAL_BATHS.pages.ALL.options.LINGER");

    [Fact]
    public void RelicFactoryPullAndRepeatCurseEvents_ExtractCleanlyWithoutGaps()
    {
        var (ranwidText, ranwidGaps) = EventMechanismReader.DescribeEventTypeForProbe(typeof(RanwidTheElder));
        Assert.Empty(ranwidGaps);
        Assert.NotNull(ranwidText);
        Assert.Contains("GiveGold: lose 100 Gold, then obtain a random relic directly", ranwidText!, StringComparison.Ordinal);
        Assert.Contains("GivePotion: discard the chosen potion without triggering its on-use effect, then obtain a random relic directly", ranwidText!, StringComparison.Ordinal);
        Assert.Contains("GiveRelic: lose/remove a relic, then obtain a random relic directly", ranwidText!, StringComparison.Ordinal);
        Assert.DoesNotContain("random outcome:", ranwidText!, StringComparison.OrdinalIgnoreCase);

        var (thisOrThatText, thisOrThatGaps) = EventMechanismReader.DescribeEventTypeForProbe(typeof(ThisOrThat));
        Assert.Empty(thisOrThatGaps);
        Assert.NotNull(thisOrThatText);
        Assert.Contains("Plain: deal unblockable damage to owner, then gain Gold", thisOrThatText!, StringComparison.Ordinal);
        Assert.Contains("Ornate: obtain a random relic directly, then add 1", thisOrThatText!, StringComparison.Ordinal);
        Assert.Contains("CARD.CLUMSY", thisOrThatText!, StringComparison.Ordinal);
        Assert.DoesNotContain("random outcome:", thisOrThatText!, StringComparison.OrdinalIgnoreCase);

        var (waterfallText, waterfallGaps) = EventMechanismReader.DescribeEventTypeForProbe(typeof(WaterfallOfObviousSecrets));
        Assert.Empty(waterfallGaps);
        Assert.NotNull(waterfallText);
        Assert.Contains("BeNice: add Dingy Dagger (CARD.DINGY_DAGGER) directly to deck", waterfallText!, StringComparison.Ordinal);
        Assert.Contains("BeatUp: obtain a random relic directly, then add 2", waterfallText!, StringComparison.Ordinal);
        Assert.Contains("CARD.GUILTY", waterfallText!, StringComparison.Ordinal);
        Assert.Contains("Negotiate: lose 70 Gold, then add card(s) directly to deck", waterfallText!, StringComparison.Ordinal);

        // Verify live completeness on WaterfallOfObviousSecrets page 1 (zero-side-effect staging) and page 2.
        var waterfall = (WaterfallOfObviousSecrets)ModelDb.Event<WaterfallOfObviousSecrets>().ToMutable();
        var enterDel = (Func<Task>)Delegate.CreateDelegate(
            typeof(Func<Task>),
            waterfall,
            typeof(WaterfallOfObviousSecrets).GetMethod("EnterWaterfall", NonPublicInstance)!);
        var (enterText, enterCompleteness) = EventMechanismReader.ReadEventWithCompleteness(
            waterfall,
            [("Enter", MakeOption(waterfall, enterDel))],
            isFakeMerchant: false);
        Assert.Null(enterText);
        Assert.Equal(MechanismCompleteness.Complete, enterCompleteness);

        var beatUpDel = (Func<Task>)Delegate.CreateDelegate(
            typeof(Func<Task>),
            waterfall,
            typeof(WaterfallOfObviousSecrets).GetMethod("BeatUp", NonPublicInstance)!);
        var negotiateDel = (Func<Task>)Delegate.CreateDelegate(
            typeof(Func<Task>),
            waterfall,
            typeof(WaterfallOfObviousSecrets).GetMethod("Negotiate", NonPublicInstance)!);
        var (page2Text, page2Completeness) = EventMechanismReader.ReadEventWithCompleteness(
            waterfall,
            [("Beat Up", MakeOption(waterfall, beatUpDel)), ("Negotiate", MakeOption(waterfall, negotiateDel))],
            isFakeMerchant: false);
        Assert.Equal(MechanismCompleteness.Complete, page2Completeness);
        Assert.NotNull(page2Text);
        Assert.Contains("Option [Beat Up]: obtain a random relic directly, then add 2", page2Text!, StringComparison.Ordinal);
        Assert.Contains("Option [Negotiate]: lose 70 Gold, then add card(s) directly to deck", page2Text!, StringComparison.Ordinal);
    }

    [Fact]
    public void BenignGuardsAndSelectorPrefsEvents_ExtractCleanlyWithoutGaps()
    {
        var (leechText, leechGaps) = EventMechanismReader.DescribeEventTypeForProbe(typeof(BrainLeech));
        Assert.Empty(leechGaps);
        Assert.NotNull(leechText);
        Assert.Contains("Rip: deal 5 unblockable damage to owner, then open an optional reward screen (card reward)", leechText!, StringComparison.Ordinal);
        Assert.Contains("ShareKnowledge: select card(s) from offered grid to add directly to deck", leechText!, StringComparison.Ordinal);

        var (automatonText, automatonGaps) = EventMechanismReader.DescribeEventTypeForProbe(typeof(InfestedAutomaton));
        Assert.Empty(automatonGaps);
        Assert.NotNull(automatonText);
        Assert.Contains("Study: add card(s) directly to deck", automatonText!, StringComparison.Ordinal);
        Assert.Contains("TouchCore: add card(s) directly to deck", automatonText!, StringComparison.Ordinal);

        var leech = (BrainLeech)ModelDb.Event<BrainLeech>().ToMutable();
        var shareDel = (Func<Task>)Delegate.CreateDelegate(
            typeof(Func<Task>),
            leech,
            typeof(BrainLeech).GetMethod("ShareKnowledge", NonPublicInstance)!);
        var (liveLeechText, liveLeechCompleteness) = EventMechanismReader.ReadEventWithCompleteness(
            leech,
            [("Share Knowledge", MakeOption(leech, shareDel))],
            isFakeMerchant: false);
        Assert.Equal(MechanismCompleteness.Complete, liveLeechCompleteness);
        Assert.Equal("Option [Share Knowledge]: select card(s) from offered grid to add directly to deck", liveLeechText);
    }

    [Fact]
    public void GenericAsyncHelpersAndCardTypeFilterEvents_ExtractCleanlyWithoutGaps()
    {
        var (bugslayerText, bugslayerGaps) = EventMechanismReader.DescribeEventTypeForProbe(typeof(Bugslayer));
        Assert.Empty(bugslayerGaps);
        Assert.NotNull(bugslayerText);
        Assert.Contains("Extermination: add", bugslayerText!, StringComparison.Ordinal);
        Assert.Contains("CARD.EXTERMINATE", bugslayerText!, StringComparison.Ordinal);
        Assert.Contains("Squash: add", bugslayerText!, StringComparison.Ordinal);
        Assert.Contains("CARD.SQUASH", bugslayerText!, StringComparison.Ordinal);

        var (bookText, bookGaps) = EventMechanismReader.DescribeEventTypeForProbe(typeof(SelfHelpBook));
        Assert.Empty(bookGaps);
        Assert.NotNull(bookText);
        Assert.Contains("ReadTheBack: select 1 Attack deck card(s) to enchant with", bookText!, StringComparison.Ordinal);
        Assert.Contains("ENCHANTMENT.SHARP (2)", bookText!, StringComparison.Ordinal);
        Assert.Contains("ReadPassage: select 1 Skill deck card(s) to enchant with", bookText!, StringComparison.Ordinal);
        Assert.Contains("ENCHANTMENT.NIMBLE (2)", bookText!, StringComparison.Ordinal);
        Assert.Contains("ReadEntireBook: select 1 Power deck card(s) to enchant with", bookText!, StringComparison.Ordinal);
        Assert.Contains("ENCHANTMENT.SWIFT (2)", bookText!, StringComparison.Ordinal);
        Assert.DoesNotContain("then enchant card(s)", bookText!, StringComparison.Ordinal);

        var book = ModelDb.Event<SelfHelpBook>();
        var readBackDel = (Func<Task>)Delegate.CreateDelegate(
            typeof(Func<Task>),
            book,
            typeof(SelfHelpBook).GetMethod("ReadTheBack", NonPublicInstance)!);
        var readPassageDel = (Func<Task>)Delegate.CreateDelegate(
            typeof(Func<Task>),
            book,
            typeof(SelfHelpBook).GetMethod("ReadPassage", NonPublicInstance)!);
        var readEntireDel = (Func<Task>)Delegate.CreateDelegate(
            typeof(Func<Task>),
            book,
            typeof(SelfHelpBook).GetMethod("ReadEntireBook", NonPublicInstance)!);
        var (liveBookText, liveBookCompleteness) = EventMechanismReader.ReadEventWithCompleteness(
            book,
            [
                ("Read the Back", MakeOption(book, readBackDel)),
                ("Read Passage", MakeOption(book, readPassageDel)),
                ("Read Entire Book", MakeOption(book, readEntireDel)),
            ],
            isFakeMerchant: false);
        Assert.Equal(MechanismCompleteness.Complete, liveBookCompleteness);
        Assert.NotNull(liveBookText);
        Assert.Contains("Option [Read the Back]: select 1 Attack deck card(s)", liveBookText!, StringComparison.Ordinal);
        Assert.Contains("Option [Read Passage]: select 1 Skill deck card(s)", liveBookText!, StringComparison.Ordinal);
        Assert.Contains("Option [Read Entire Book]: select 1 Power deck card(s)", liveBookText!, StringComparison.Ordinal);
    }

    [Fact]
    public void FutureOfPotions_InspectsAfterGeneratedUpgradeHandlerAndFailsClosedOnOtherHandlers()
    {
        var (fopText, fopGaps) = EventMechanismReader.DescribeEventTypeForProbe(typeof(TheFutureOfPotions));
        Assert.Empty(fopGaps);
        Assert.NotNull(fopText);
        Assert.Contains(
            "Trade: discard the chosen potion without triggering its on-use effect, then open an optional reward screen (upgraded card reward)",
            fopText!,
            StringComparison.Ordinal);

        // Synthetic non-upgrade AfterGenerated handler must fail closed.
        var synthetic = new SyntheticUnexpectedAfterGeneratedEvent();
        var method = typeof(SyntheticUnexpectedAfterGeneratedEvent).GetMethod(
            nameof(SyntheticUnexpectedAfterGeneratedEvent.OfferWithSideEffect),
            BindingFlags.Instance | BindingFlags.Public)!;
        var (summary, gap) = EventMechanismReader.DescribeOptionMethod(synthetic, null, method);
        Assert.Null(summary);
        Assert.Equal("unresolved-call:CardReward.add_AfterGenerated", gap);

        // Synthetic conditional AfterGenerated upgrade handler must also fail closed.
        var conditionalMethod = typeof(SyntheticUnexpectedAfterGeneratedEvent).GetMethod(
            nameof(SyntheticUnexpectedAfterGeneratedEvent.OfferWithConditionalUpgrade),
            BindingFlags.Instance | BindingFlags.Public)!;
        var (condSummary, condGap) = EventMechanismReader.DescribeOptionMethod(synthetic, null, conditionalMethod);
        Assert.Null(condSummary);
        Assert.Equal("unresolved-call:CardReward.add_AfterGenerated", condGap);
    }

    [Fact]
    public void AbyssalBaths_AllowsChainedSelfLoopContinuationWhileColossalFlowerRemainsFailClosed()
    {
        var (bathsText, bathsGaps) = EventMechanismReader.DescribeEventTypeForProbe(typeof(AbyssalBaths));
        Assert.Empty(bathsGaps);
        Assert.NotNull(bathsText);
        Assert.Contains("Immerse: gain 2 Max HP, then deal 3 unblockable damage to owner", bathsText!, StringComparison.Ordinal);
        Assert.Contains("Abstain: heal 10 HP", bathsText!, StringComparison.Ordinal);
        Assert.Contains("Linger: gain 2 Max HP, then deal 3 unblockable damage to owner", bathsText!, StringComparison.Ordinal);

        var baths = (AbyssalBaths)ModelDb.Event<AbyssalBaths>().ToMutable();
        var immerseDel = (Func<Task>)Delegate.CreateDelegate(
            typeof(Func<Task>),
            baths,
            typeof(AbyssalBaths).GetMethod("Immerse", NonPublicInstance)!);
        var abstainDel = (Func<Task>)Delegate.CreateDelegate(
            typeof(Func<Task>),
            baths,
            typeof(AbyssalBaths).GetMethod("Abstain", NonPublicInstance)!);
        var (liveBathsText, liveBathsCompleteness) = EventMechanismReader.ReadEventWithCompleteness(
            baths,
            [("Immerse", MakeOption(baths, immerseDel)), ("Abstain", MakeOption(baths, abstainDel))],
            isFakeMerchant: false);
        Assert.Equal(MechanismCompleteness.Complete, liveBathsCompleteness);
        Assert.NotNull(liveBathsText);
        Assert.Contains("Option [Immerse]: gain 2 Max HP, then deal 3 unblockable damage to owner", liveBathsText!, StringComparison.Ordinal);
        Assert.Contains("Option [Abstain]: heal 10 HP", liveBathsText!, StringComparison.Ordinal);

        // ColossalFlower.ReachDeeper must remain fail-closed with multi-stage-event-state.
        var flower = ModelDb.Event<ColossalFlower>();
        var reachDeeper = typeof(ColossalFlower).GetMethod("ReachDeeper", NonPublicInstance)!;
        var (reachSummary, reachGap) = EventMechanismReader.DescribeOptionMethod(flower, null, reachDeeper);
        Assert.Null(reachSummary);
        Assert.Equal("multi-stage-event-state", reachGap);
    }

    [Fact]
    public void Reflections_ExtractsCompoundCountedAndDeckCloneLoopsWithoutGaps()
    {
        var (reflectionsText, reflectionsGaps) = EventMechanismReader.DescribeEventTypeForProbe(typeof(Reflections));
        Assert.Empty(reflectionsGaps);
        Assert.NotNull(reflectionsText);
        Assert.Contains(
            "TouchAMirror: downgrade up to 2 random upgraded card(s) in deck, then upgrade up to 4 random upgradable card(s) in deck",
            reflectionsText!,
            StringComparison.Ordinal);
        Assert.Contains(
            "Shatter: duplicate all deck cards directly into deck, then add 1",
            reflectionsText!,
            StringComparison.Ordinal);
        Assert.Contains("CARD.BAD_LUCK", reflectionsText!, StringComparison.Ordinal);

        var reflections = (Reflections)ModelDb.Event<Reflections>().ToMutable();
        var touchDel = (Func<Task>)Delegate.CreateDelegate(
            typeof(Func<Task>),
            reflections,
            typeof(Reflections).GetMethod("TouchAMirror", NonPublicInstance)!);
        var shatterDel = (Func<Task>)Delegate.CreateDelegate(
            typeof(Func<Task>),
            reflections,
            typeof(Reflections).GetMethod("Shatter", NonPublicInstance)!);
        var (liveText, liveCompleteness) = EventMechanismReader.ReadEventWithCompleteness(
            reflections,
            [("Touch a Mirror", MakeOption(reflections, touchDel)), ("Shatter", MakeOption(reflections, shatterDel))],
            isFakeMerchant: false);
        Assert.Equal(MechanismCompleteness.Complete, liveCompleteness);
        Assert.NotNull(liveText);
        Assert.Contains(
            "Option [Touch a Mirror]: downgrade up to 2 random upgraded card(s) in deck, then upgrade up to 4 random upgradable card(s) in deck",
            liveText!,
            StringComparison.Ordinal);
        Assert.Contains("Option [Shatter]: duplicate all deck cards directly into deck, then add 1", liveText!, StringComparison.Ordinal);
    }

    [Fact]
    public void MuseumOfTortureAndTinkerTimeStage3_ExtractCleanlyWhileFailClosedControlsHold()
    {
        // MuseumOfTorture pages 1-2 (EnterMuseum, ViewDisplay) are presentation-only + zero-side-effect staging.
        var (museumText, museumGaps) = EventMechanismReader.DescribeEventTypeForProbe(typeof(MuseumOfTorture));
        Assert.Empty(museumGaps);
        Assert.NotNull(museumText);
        Assert.Contains("TakeRelic: random outcome: obtain a relic directly, then add card(s) directly to deck", museumText!, StringComparison.Ordinal);
        Assert.Contains("BreakOut: lose 10 Max HP", museumText!, StringComparison.Ordinal);

        var museum = (MuseumOfTorture)ModelDb.Event<MuseumOfTorture>().ToMutable();
        var enterDel = (Func<Task>)Delegate.CreateDelegate(
            typeof(Func<Task>),
            museum,
            typeof(MuseumOfTorture).GetMethod("EnterMuseum", NonPublicInstance)!);
        var (p1Text, p1Completeness) = EventMechanismReader.ReadEventWithCompleteness(
            museum,
            [("Enter", MakeOption(museum, enterDel))],
            isFakeMerchant: false);
        Assert.Null(p1Text);
        Assert.Equal(MechanismCompleteness.Complete, p1Completeness);

        var viewDel = (Func<Task>)Delegate.CreateDelegate(
            typeof(Func<Task>),
            museum,
            typeof(MuseumOfTorture).GetMethod("ViewDisplay", NonPublicInstance)!);
        var (p2Text, p2Completeness) = EventMechanismReader.ReadEventWithCompleteness(
            museum,
            [("Continue", MakeOption(museum, viewDel))],
            isFakeMerchant: false);
        Assert.Null(p2Text);
        Assert.Equal(MechanismCompleteness.Complete, p2Completeness);

        // TinkerTime Stage 3 (RiderChosen and its closure wrapper) extracts MadScience cleanly.
        var tinker = (TinkerTime)ModelDb.Event<TinkerTime>().ToMutable();
        var riderChosen = typeof(TinkerTime).GetMethod("RiderChosen", NonPublicInstance)!;
        var (riderSummary, riderGap) = EventMechanismReader.DescribeOptionMethod(tinker, null, riderChosen);
        Assert.Null(riderGap);
        Assert.NotNull(riderSummary);
        Assert.Contains("add", riderSummary!, StringComparison.Ordinal);
        Assert.Contains("CARD.MAD_SCIENCE", riderSummary!, StringComparison.Ordinal);
        Assert.Contains("directly to deck", riderSummary!, StringComparison.Ordinal);

        var closureType = typeof(TinkerTime).GetNestedType("<>c__DisplayClass13_0", BindingFlags.NonPublic)!;
        var closureInstance = Activator.CreateInstance(closureType, nonPublic: true)!;
        var closureMethod = closureType.GetMethod("<ChooseRiderEffect>b__0", NonPublicInstance)!;
        var closureDelegate = (Func<Task>)Delegate.CreateDelegate(typeof(Func<Task>), closureInstance, closureMethod);
        var (stage3Text, stage3Completeness) = EventMechanismReader.ReadEventWithCompleteness(
            tinker,
            [("Rider Option", MakeOption(tinker, closureDelegate))],
            isFakeMerchant: false);
        Assert.Equal(MechanismCompleteness.Complete, stage3Completeness);
        Assert.NotNull(stage3Text);
        Assert.Contains("Option [Rider Option]: add", stage3Text!, StringComparison.Ordinal);
        Assert.Contains("CARD.MAD_SCIENCE", stage3Text!, StringComparison.Ordinal);

        // Fail-closed controls: CrystalSphere, ColossalFlower.ReachDeeper, SlipperyBridge.HoldOn.
        var (_, sphereGaps) = EventMechanismReader.DescribeEventTypeForProbe(typeof(CrystalSphere));
        Assert.Contains("UncoverFuture/unresolved-call:CrystalSphereMinigame.PlayMinigame", sphereGaps);
        Assert.Contains("PaymentPlan/unresolved-call:CrystalSphereMinigame.PlayMinigame", sphereGaps);

        var (_, flowerGaps) = EventMechanismReader.DescribeEventTypeForProbe(typeof(ColossalFlower));
        Assert.Contains("ReachDeeper/multi-stage-event-state", flowerGaps);

        var (_, bridgeGaps) = EventMechanismReader.DescribeEventTypeForProbe(typeof(SlipperyBridge));
        Assert.Contains("HoldOn/unresolved-helper:GetNewRandomCard", bridgeGaps);
    }

    private sealed class SyntheticUnexpectedAfterGeneratedEvent : EventModel
    {
        protected override IReadOnlyList<EventOption> GenerateInitialOptions() => [];

        public async Task OfferWithSideEffect()
        {
            var reward = new CardReward(default!, 3, Owner!);
            reward.AfterGenerated += () =>
            {
                _ = PlayerCmd.GainGold(50m, Owner!);
            };
            await RewardsCmd.OfferCustom(Owner!, [reward]);
        }

        public async Task OfferWithConditionalUpgrade()
        {
            var reward = new CardReward(default!, 3, Owner!);
            reward.AfterGenerated += () =>
            {
                foreach (var card in reward.Cards)
                {
                    if (card.Type == CardType.Attack)
                        CardCmd.Upgrade(card);
                }
            };
            await RewardsCmd.OfferCustom(Owner!, [reward]);
        }
    }
}
