using System.Globalization;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.Loader;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Entities.Ascension;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Events;
using MegaCrit.Sts2.Core.GameActions.Multiplayer;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Enchantments;
using MegaCrit.Sts2.Core.Models.Events;
using MegaCrit.Sts2.Core.Models.Monsters;
using MegaCrit.Sts2.Core.Models.Potions;
using MegaCrit.Sts2.Core.Models.Powers;
using MegaCrit.Sts2.Core.MonsterMoves;
using MegaCrit.Sts2.Core.MonsterMoves.Intents;
using MegaCrit.Sts2.Core.MonsterMoves.MonsterMoveStateMachine;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.ValueProps;
using Sts2Mod.Observation;
using Xunit;

namespace Sts2Mod.Native.Tests;

internal static class NativeTestBootstrap
{
    private static readonly object Gate = new();
    private static bool _initialized;

    [ModuleInitializer]
    internal static void RegisterResolver()
    {
        var gameDir = typeof(NativeTestBootstrap).Assembly
            .GetCustomAttributes<AssemblyMetadataAttribute>()
            .FirstOrDefault(a => a.Key == "Sts2GameDir")?.Value;
        if (string.IsNullOrWhiteSpace(gameDir))
            return;

        AssemblyLoadContext.Default.Resolving += (_, assembly) =>
        {
            var path = Path.Combine(gameDir, assembly.Name + ".dll");
            return File.Exists(path) ? AssemblyLoadContext.Default.LoadFromAssemblyPath(path) : null;
        };
    }

    internal static void EnsureModelDb()
    {
        lock (Gate)
        {
            if (_initialized)
                return;

            const BindingFlags hidden = BindingFlags.NonPublic | BindingFlags.Instance;
            var loc = (LocManager)RuntimeHelpers.GetUninitializedObject(typeof(LocManager));
            var tables = new[]
            {
                "monsters", "powers", "afflictions", "cards", "relics", "potions",
                "enchantments", "orbs", "events", "ancients", "encounters",
            }.ToDictionary(key => key, key => new LocTable(key, key switch
            {
                "cards" => new Dictionary<string, string>
                {
                    ["PECK.title"] = "Peck",
                    ["TORIC_TOUGHNESS.title"] = "Toric Toughness",
                },
                "enchantments" => new Dictionary<string, string>
                {
                    ["SLITHER.title"] = "Slither",
                },
                "events" => new Dictionary<string, string>
                {
                    ["ABYSSAL_BATHS.pages.ALL.options.LINGER.title"] = "Linger",
                    ["ABYSSAL_BATHS.pages.ALL.options.LINGER.description"] = "Linger",
                },
                _ => [],
            }));
            typeof(LocManager).GetField("_tables", hidden)!.SetValue(loc, tables);
            typeof(LocManager).GetProperty("Instance")!.SetValue(null, loc);
            typeof(LocManager).GetProperty("Language")!.SetValue(loc, "eng");
            typeof(LocManager).GetProperty("CultureInfo")!.SetValue(loc, CultureInfo.InvariantCulture);
            typeof(LocManager).GetMethod("LoadLocFormatters", hidden)!.Invoke(loc, null);

            var game = typeof(MonsterModel).Assembly;
            foreach (var type in game.GetTypes().Where(t =>
                !t.IsAbstract && !t.IsNested && t.GetConstructor(Type.EmptyTypes) is not null
                && (typeof(MonsterModel).IsAssignableFrom(t)
                    || typeof(PowerModel).IsAssignableFrom(t)
                    || typeof(AfflictionModel).IsAssignableFrom(t)
                    || typeof(CardModel).IsAssignableFrom(t)
                    || typeof(RelicModel).IsAssignableFrom(t)
                    || typeof(PotionModel).IsAssignableFrom(t)
                    || typeof(EnchantmentModel).IsAssignableFrom(t)
                    || typeof(OrbModel).IsAssignableFrom(t)
                    || typeof(EventModel).IsAssignableFrom(t)
                    || typeof(EncounterModel).IsAssignableFrom(t))))
            {
                try { ModelDb.Inject(type); }
                catch { /* Some models require live Godot nodes; skip safely for offline tests. */ }
            }

            var run = (RunState)RuntimeHelpers.GetUninitializedObject(typeof(RunState));
            typeof(RunState).GetProperty("GameMode")!.SetValue(run, GameMode.Standard);
            typeof(RunState).GetProperty("AscensionLevel")!.SetValue(run, 0);
            typeof(RunManager).GetProperty("State", hidden)!.SetValue(RunManager.Instance, run);
            typeof(RunManager).GetProperty("AscensionManager")!.SetValue(RunManager.Instance, new AscensionManager(GameMode.Standard, 0));

            _initialized = true;
        }
    }
}

public sealed class NativeExtractionTests
{
    public NativeExtractionTests()
    {
        NativeTestBootstrap.EnsureModelDb();
    }

    [Fact]
    public void CountedLoop_PreservesPerIterationEffectOrderAndPairing()
    {
        var ovicopter = ModelDb.Monster<Ovicopter>().ToMutable();
        ovicopter.SetUpForCombat();
        var (text, gaps) = EnemyMechanismReader.Describe(ovicopter);
        Assert.Empty(gaps);
        Assert.NotNull(text);

        var layEggsLine = text!.Split('\n').Single(l => l.StartsWith("LAY_EGGS_MOVE:", StringComparison.Ordinal));
        // Must wrap the full per-iteration body in a single `3 times:` block rather than repeating each step separately.
        Assert.Contains("3 times: if an encounter slot is empty: (summon ", layEggsLine, StringComparison.Ordinal);
        Assert.Contains("POWER.MINION_POWER", layEggsLine, StringComparison.Ordinal);
        Assert.Equal(1, layEggsLine.Split("3 times:", StringSplitOptions.None).Length - 1);
    }

    [Fact]
    public void EventOptions_FailClosedOnUnresolvedDelegateAndPreserveTransformTargets()
    {
        var conveyor = (EndlessConveyor)ModelDb.Event<EndlessConveyor>().ToMutable();
        var grabMethod = typeof(EndlessConveyor).GetMethod("GrabSomethingOffTheBelt", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var (grabSummary, grabGap) = EventMechanismReader.DescribeOptionMethod(conveyor, null, grabMethod);
        Assert.Null(grabSummary);
        Assert.NotNull(grabGap);

        var carvings = (WoodCarvings)ModelDb.Event<WoodCarvings>().ToMutable();
        var birdMethod = typeof(WoodCarvings).GetMethod("Bird", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var torusMethod = typeof(WoodCarvings).GetMethod("Torus", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var (birdSummary, birdGap) = EventMechanismReader.DescribeOptionMethod(carvings, null, birdMethod);
        var (torusSummary, torusGap) = EventMechanismReader.DescribeOptionMethod(carvings, null, torusMethod);

        Assert.Null(birdGap);
        Assert.NotNull(birdSummary);
        Assert.Contains("CARD.PECK", birdSummary!, StringComparison.Ordinal);

        Assert.Null(torusGap);
        Assert.NotNull(torusSummary);
        Assert.Contains("CARD.TORIC_TOUGHNESS", torusSummary!, StringComparison.Ordinal);
    }

    [Fact]
    public void EventOption_RefreshesDynamicDamageAcrossConsecutiveObservations()
    {
        var baths = (AbyssalBaths)ModelDb.Event<AbyssalBaths>().ToMutable();
        var lingerMethod = typeof(AbyssalBaths).GetMethod("Linger", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var lingerDelegate = (Func<Task>)Delegate.CreateDelegate(typeof(Func<Task>), baths, lingerMethod);
        var option = new EventOption(baths, lingerDelegate, "ABYSSAL_BATHS.pages.ALL.options.LINGER");
        var options = new[] { ("Linger", option) };

        baths.DynamicVars.Damage.BaseValue = 4m;
        var first = EventMechanismReader.ReadEvent(baths, options, isFakeMerchant: false);
        Assert.NotNull(first);
        Assert.Contains("deal 4 unblockable damage", first!, StringComparison.Ordinal);

        baths.DynamicVars.Damage.BaseValue = 5m;
        var second = EventMechanismReader.ReadEvent(baths, options, isFakeMerchant: false);
        Assert.NotNull(second);
        Assert.Contains("deal 5 unblockable damage", second!, StringComparison.Ordinal);
        Assert.DoesNotContain("deal 4 unblockable damage", second!, StringComparison.Ordinal);
    }

    [Fact]
    public void Enchantments_ReportExactNativeFormulaAndTiming()
    {
        var (goopyText, goopyGaps) = ModifierMechanismReader.DescribeEnchantmentWithGaps(typeof(Goopy));
        Assert.Empty(goopyGaps);
        Assert.NotNull(goopyText);
        Assert.Contains("+(amount - 1) Block", goopyText!, StringComparison.Ordinal);
        Assert.DoesNotContain("+amount Block", goopyText!, StringComparison.Ordinal);

        var (momentumText, momentumGaps) = ModifierMechanismReader.DescribeEnchantmentWithGaps(typeof(Momentum));
        Assert.Empty(momentumGaps);
        Assert.NotNull(momentumText);
        Assert.Contains("accumulated bonus", momentumText!, StringComparison.Ordinal);
        Assert.DoesNotContain("Adds +amount", momentumText!, StringComparison.Ordinal);

        var (perfectFitText, perfectFitGaps) = ModifierMechanismReader.DescribeEnchantmentWithGaps(typeof(PerfectFit));
        Assert.Empty(perfectFitGaps);
        Assert.NotNull(perfectFitText);
        Assert.Contains("non-initial shuffles", perfectFitText!, StringComparison.Ordinal);

        var (emberText, emberGaps) = ModifierMechanismReader.DescribeEnchantmentWithGaps(typeof(TezcatarasEmber));
        Assert.Empty(emberGaps);
        Assert.NotNull(emberText);
        Assert.Contains("+3 powered attack damage", emberText!, StringComparison.Ordinal);

        var (sharpText, sharpGaps) = ModifierMechanismReader.DescribeEnchantmentWithGaps(typeof(Sharp));
        Assert.Empty(sharpGaps);
        Assert.Contains("+amount powered attack damage", sharpText!, StringComparison.Ordinal);
    }

    [Fact]
    public void MasterPlannerPower_ResolvesPlayedCardTargetAndSlyKeyword()
    {
        var (text, gaps) = PowerMechanismReader.Describe(typeof(MasterPlannerPower), true);
        Assert.Empty(gaps);
        Assert.NotNull(text);
        Assert.Contains("apply Sly to played card", text!, StringComparison.Ordinal);
        Assert.DoesNotContain("generated card", text!, StringComparison.Ordinal);
    }

    [Fact]
    public void SpecialMoves_MatchNativeOrderAndPriorityPredicates()
    {
        var rat = ModelDb.Monster<TwoTailedRat>().ToMutable();
        rat.SetUpForCombat();
        var (ratText, ratGaps) = EnemyMechanismReader.Describe(rat);
        Assert.Empty(ratGaps);
        var backupLine = ratText!.Split('\n').Single(l => l.StartsWith("CALL_FOR_BACKUP_MOVE:", StringComparison.Ordinal));
        var summonIdx = backupLine.IndexOf("if an encounter slot is empty: summon ", StringComparison.Ordinal);
        var countIdx = backupLine.IndexOf("; set shared call for backup count", StringComparison.Ordinal);
        Assert.True(summonIdx >= 0 && countIdx > summonIdx, $"Unexpected CallForBackup wording: {backupLine}");

        var hopper = ModelDb.Monster<ThievingHopper>().ToMutable();
        hopper.SetUpForCombat();
        var (hopperText, hopperGaps) = EnemyMechanismReader.Describe(hopper);
        Assert.Empty(hopperGaps);
        var stealLine = hopperText!.Split('\n').Single(l => l.StartsWith("THIEVERY_MOVE:", StringComparison.Ordinal));
        Assert.Contains("non-ENCHANTMENT.IMBUED Uncommon", stealLine, StringComparison.Ordinal);
        Assert.Contains("non-ENCHANTMENT.IMBUED Common/Rare/Event", stealLine, StringComparison.Ordinal);
        Assert.Contains("non-ENCHANTMENT.IMBUED Basic", stealLine, StringComparison.Ordinal);
        Assert.Contains("Ancient or ENCHANTMENT.IMBUED", stealLine, StringComparison.Ordinal);
        Assert.DoesNotContain("unupgraded non-basic", stealLine, StringComparison.Ordinal);
    }

    [Fact]
    public void ExactRngDomain_PropagatesBoundsAndRejectsOversizedDomains()
    {
        var rng = typeof(EncounterModel).GetProperty("Rng", BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public)!.GetMethod!;
        var rngGetter = new NativeRuleBody.Call(rng, NativeRuleBody.Self, []);
        var nextInt2 = typeof(MegaCrit.Sts2.Core.Random.Rng).GetMethod("NextInt", [typeof(int), typeof(int)])!;

        var bounded = new NativeRuleBody.Call(nextInt2, rngGetter, [new NativeRuleBody.Constant(2), new NativeRuleBody.Constant(5)]);
        Assert.True(EncounterMechanismReader.TryReadExactRngDomain(bounded, out _, out _, out var domain));
        Assert.Equal(new[] { 2, 3, 4 }, domain);

        var oversized = new NativeRuleBody.Call(nextInt2, rngGetter, [new NativeRuleBody.Constant(0), new NativeRuleBody.Constant(100)]);
        Assert.False(EncounterMechanismReader.TryReadExactRngDomain(oversized, out _, out _, out _));
    }

    [Fact]
    public void ActTransition_UsesBlitzAscensionMapping()
    {
        Assert.True(EventMechanismReader.HasWearyTraveler(1, GameMode.Blitz));
        Assert.False(EventMechanismReader.HasWearyTraveler(1, GameMode.Standard));
        Assert.True(EventMechanismReader.HasWearyTraveler(2, GameMode.Standard));
    }

    [Fact]
    public void ModelVisibleText_OmitsEngineNamesAndDescribesBlockableVsUnblockableDamage()
    {
        var fairy = ModelDb.Potion<FairyInABottle>();
        var fairyRule = ItemMechanismReader.DescribePotion(fairy);
        Assert.NotNull(fairyRule);
        Assert.DoesNotContain("ShouldDieLate", fairyRule!, StringComparison.Ordinal);

        var dummyEventType = typeof(EventModel).Assembly.GetTypes().FirstOrDefault(t => t.Name == "BattlewornDummy");
        if (dummyEventType is not null)
        {
            var (dummyText, _) = EventMechanismReader.DescribeEventTypeForProbe(dummyEventType);
            if (dummyText is not null)
                Assert.DoesNotContain("BattlewornDummyEventV1Encounter", dummyText, StringComparison.Ordinal);
        }

        var xRule = MacroMechanismReader.XCostRule();
        Assert.Contains("X-value modifiers", xRule, StringComparison.Ordinal);
        Assert.Contains("auto-plays spend 0 Energy", xRule, StringComparison.Ordinal);

        var dummySynthetic = new SyntheticDamageEvent();
        var blockableMethod = typeof(SyntheticDamageEvent).GetMethod(nameof(SyntheticDamageEvent.BlockableHit), BindingFlags.Instance | BindingFlags.Public)!;
        var unblockableMethod = typeof(SyntheticDamageEvent).GetMethod(nameof(SyntheticDamageEvent.UnblockableHit), BindingFlags.Instance | BindingFlags.Public)!;
        var (blockableSummary, blockableGap) = EventMechanismReader.DescribeOptionMethod(dummySynthetic, null, blockableMethod);
        var (unblockableSummary, unblockableGap) = EventMechanismReader.DescribeOptionMethod(dummySynthetic, null, unblockableMethod);
        Assert.Null(blockableGap);
        Assert.Null(unblockableGap);
        Assert.Contains("deal 7 blockable damage", blockableSummary!, StringComparison.Ordinal);
        Assert.Contains("deal 7 unblockable damage", unblockableSummary!, StringComparison.Ordinal);
    }

    [Fact]
    public void RecursiveGetters_TerminateWithGapInsteadOfStackOverflow()
    {
        var monster = new SyntheticCyclicMonster();
        var reader = new NativeRuleText(monster);
        var condMethod = typeof(SyntheticCyclicMonster).GetMethod(nameof(SyntheticCyclicMonster.CheckCyclic), BindingFlags.Instance | BindingFlags.Public)!;
        var result = reader.ReadCondition(condMethod);
        Assert.Null(result);
        Assert.NotEmpty(reader.Gaps);
    }

    [Fact]
    public void BranchWithMissingDestination_OmitsIncompleteCandidateListAndRecordsGap()
    {
        var monster = new SyntheticBrokenBranchMonster();
        typeof(AbstractModel).GetProperty("IsMutable")!.SetValue(monster, true);
        monster.SetUpForCombat();
        var (text, gaps) = EnemyMechanismReader.Describe(monster);
        Assert.NotEmpty(gaps);
        Assert.True(text is null || !text.Contains("BROKEN_BRANCH: one of", StringComparison.Ordinal));
    }

    private sealed class SyntheticDamageEvent : EventModel
    {
        protected override IReadOnlyList<EventOption> GenerateInitialOptions() => [];

        public Task BlockableHit()
            => CreatureCmd.Damage(new ThrowingPlayerChoiceContext(), Owner!.Creature, 7m, ValueProp.Unpowered, null, null);

        public Task UnblockableHit()
            => CreatureCmd.Damage(new ThrowingPlayerChoiceContext(), Owner!.Creature, 7m, ValueProp.Unblockable | ValueProp.Unpowered, null, null);
    }

    private sealed class SyntheticCyclicMonster : MonsterModel
    {
        public override int MinInitialHp => 10;
        public override int MaxInitialHp => 10;
        private bool PropA => PropB;
        private bool PropB => PropA;
        public bool CheckCyclic() => PropA;
        protected override MonsterMoveStateMachine GenerateMoveStateMachine()
            => new([], new MoveState("IDLE", _ => Task.CompletedTask, new SleepIntent()));
    }

    private sealed class SyntheticBrokenBranchMonster : MonsterModel
    {
        public override int MinInitialHp => 10;
        public override int MaxInitialHp => 10;

        protected override MonsterMoveStateMachine GenerateMoveStateMachine()
        {
            var valid = new MoveState("VALID_MOVE", _ => Task.CompletedTask, new SleepIntent());
            var unregistered = new MoveState("MISSING_STATE_ID", _ => Task.CompletedTask, new SleepIntent());
            var branch = new RandomBranchState("BROKEN_BRANCH");
            valid.FollowUpState = branch;
            branch.AddBranch(valid, MoveRepeatType.CanRepeatForever, () => 1f);
            branch.AddBranch(unregistered, MoveRepeatType.CanRepeatForever, () => 1f);
            return new MonsterMoveStateMachine([valid, branch], valid);
        }
    }
}
