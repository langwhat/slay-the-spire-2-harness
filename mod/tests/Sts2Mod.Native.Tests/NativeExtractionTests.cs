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
using MegaCrit.Sts2.Core.Models.Afflictions;
using MegaCrit.Sts2.Core.Models.Cards;
using MegaCrit.Sts2.Core.Models.Enchantments;
using MegaCrit.Sts2.Core.Models.Events;
using MegaCrit.Sts2.Core.Models.Monsters;
using MegaCrit.Sts2.Core.Models.Orbs;
using MegaCrit.Sts2.Core.Models.Potions;
using MegaCrit.Sts2.Core.Models.Powers;
using MegaCrit.Sts2.Core.Models.Relics;
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
                "enchantments", "orbs", "events", "ancients", "encounters", "card_keywords",
            }.ToDictionary(key => key, key => new LocTable(key, key switch
            {
                "cards" => new Dictionary<string, string>
                {
                    ["PECK.title"] = "Peck",
                    ["TORIC_TOUGHNESS.title"] = "Toric Toughness",
                    ["DEBT.title"] = "Debt",
                    ["ULTIMATE_STRIKE.title"] = "Ultimate Strike",
                    ["ULTIMATE_DEFEND.title"] = "Ultimate Defend",
                    ["DINGY_DAGGER.title.dingy"] = "Dingy Dagger",
                },
                "enchantments" => new Dictionary<string, string>
                {
                    ["SLITHER.title"] = "Slither",
                },
                "card_keywords" => new Dictionary<string, string>
                {
                    ["sly.title"] = "Sly",
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
        Assert.Contains(
            "3 times: if an encounter slot is empty: (summon MONSTER.TOUGH_EGG; attempt to apply 1 POWER.MINION_POWER to the summoned MONSTER.TOUGH_EGG)",
            layEggsLine,
            StringComparison.Ordinal);
        Assert.Equal(1, layEggsLine.Split("3 times:", StringSplitOptions.None).Length - 1);
    }

    [Fact]
    public void InfestedPowerSummon_PreservesStartStunnedOnSummonedWriggler()
    {
        var phrog = ModelDb.Monster<PhrogParasite>().ToMutable();
        phrog.SetUpForCombat();
        var (text, gaps) = EnemyMechanismReader.Describe(phrog);
        Assert.Empty(gaps);
        Assert.NotNull(text);
        Assert.Contains("Summoned MONSTER.WRIGGLER:", text!, StringComparison.Ordinal);
        Assert.Contains("  First move: SPAWNED_MOVE", text!, StringComparison.Ordinal);
    }

    [Fact]
    public void EventOptions_FailClosedOnUnresolvedCallsAndExtractCountsAndFiltersAccurately()
    {
        // Unconditional unknown call/delegate must fail closed with a gap and omit the option summary.
        var synthetic = new SyntheticUnresolvedCallEvent();
        var unknownCallMethod = typeof(SyntheticUnresolvedCallEvent).GetMethod(nameof(SyntheticUnresolvedCallEvent.PayThenUnknown), BindingFlags.Instance | BindingFlags.Public)!;
        var (unknownSummary, unknownGap) = EventMechanismReader.DescribeOptionMethod(synthetic, null, unknownCallMethod);
        Assert.Null(unknownSummary);
        Assert.NotNull(unknownGap);

        // CrystalSphere minigame options must fail closed rather than emitting cost-only half transactions.
        var sphere = ModelDb.Event<CrystalSphere>();
        var uncover = typeof(CrystalSphere).GetMethod("UncoverFuture", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var paymentPlan = typeof(CrystalSphere).GetMethod("PaymentPlan", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var (uncoverSummary, uncoverGap) = EventMechanismReader.DescribeOptionMethod(sphere, null, uncover);
        var (planSummary, planGap) = EventMechanismReader.DescribeOptionMethod(sphere, null, paymentPlan);
        Assert.Null(uncoverSummary);
        Assert.NotNull(uncoverGap);
        Assert.Null(planSummary);
        Assert.NotNull(planGap);

        // EndlessConveyor conditional cost + delegate must fail closed.
        var conveyor = ModelDb.Event<EndlessConveyor>();
        var grabMethod = typeof(EndlessConveyor).GetMethod("GrabSomethingOffTheBelt", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var (grabSummary, grabGap) = EventMechanismReader.DescribeOptionMethod(conveyor, null, grabMethod);
        Assert.Null(grabSummary);
        Assert.NotNull(grabGap);

        // WaterloggedScriptorium PricklySponge must report 2 cards from CardSelectorPrefs (not enchantment amount 1).
        var scriptorium = ModelDb.Event<WaterloggedScriptorium>();
        var spongeMethod = typeof(WaterloggedScriptorium).GetMethod("PricklySponge", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var (spongeSummary, spongeGap) = EventMechanismReader.DescribeOptionMethod(scriptorium, null, spongeMethod);
        Assert.Null(spongeGap);
        Assert.NotNull(spongeSummary);
        Assert.Contains("select 2 eligible deck card(s)", spongeSummary!, StringComparison.Ordinal);
        Assert.DoesNotContain("select 1", spongeSummary!, StringComparison.Ordinal);

        // WoodCarvings Bird and Torus must keep the Basic filter and transform target cards.
        var carvings = ModelDb.Event<WoodCarvings>();
        var birdMethod = typeof(WoodCarvings).GetMethod("Bird", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var torusMethod = typeof(WoodCarvings).GetMethod("Torus", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var (birdSummary, birdGap) = EventMechanismReader.DescribeOptionMethod(carvings, null, birdMethod);
        var (torusSummary, torusGap) = EventMechanismReader.DescribeOptionMethod(carvings, null, torusMethod);
        Assert.Null(birdGap);
        Assert.Contains("transformable Basic", birdSummary!, StringComparison.Ordinal);
        Assert.Contains("CARD.PECK", birdSummary!, StringComparison.Ordinal);
        Assert.Null(torusGap);
        Assert.Contains("transformable Basic", torusSummary!, StringComparison.Ordinal);
        Assert.Contains("CARD.TORIC_TOUGHNESS", torusSummary!, StringComparison.Ordinal);

        // StoneOfAllTime discards Rng.NextInt(100); deterministic options must NOT be tagged as random outcome,
        // and Push must include the 8 Vigorous stacks.
        var stone = ModelDb.Event<StoneOfAllTime>();
        var liftMethod = typeof(StoneOfAllTime).GetMethod("Lift", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var pushMethod = typeof(StoneOfAllTime).GetMethod("Push", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var (liftSummary, liftGap) = EventMechanismReader.DescribeOptionMethod(stone, null, liftMethod);
        var (pushSummary, pushGap) = EventMechanismReader.DescribeOptionMethod(stone, null, pushMethod);
        Assert.Null(liftGap);
        Assert.Null(pushGap);
        Assert.DoesNotContain("random outcome", liftSummary!, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("random outcome", pushSummary!, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("ENCHANTMENT.VIGOROUS (8)", pushSummary!, StringComparison.Ordinal);

        // ColossalFlower.ReachDeeper unlocks higher future prizes via SetEventState and must fail closed instead of showing damage-only.
        var flower = ModelDb.Event<ColossalFlower>();
        var reachDeeper = typeof(ColossalFlower).GetMethod("ReachDeeper", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var (reachSummary, reachGap) = EventMechanismReader.DescribeOptionMethod(flower, null, reachDeeper);
        Assert.Null(reachSummary);
        Assert.NotNull(reachGap);

        // Amalgamator must distinguish Basic Strike -> UltimateStrike from Basic Defend -> UltimateDefend.
        var amalgamator = ModelDb.Event<Amalgamator>();
        var combineStrikes = typeof(Amalgamator).GetMethod("CombineStrikes", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var combineDefends = typeof(Amalgamator).GetMethod("CombineDefends", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var (strikesSummary, strikesGap) = EventMechanismReader.DescribeOptionMethod(amalgamator, null, combineStrikes);
        var (defendsSummary, defendsGap) = EventMechanismReader.DescribeOptionMethod(amalgamator, null, combineDefends);
        Assert.Null(strikesGap);
        Assert.Contains("Basic Strike", strikesSummary!, StringComparison.Ordinal);
        Assert.Contains("CARD.ULTIMATE_STRIKE", strikesSummary!, StringComparison.Ordinal);
        Assert.Null(defendsGap);
        Assert.Contains("Basic Defend", defendsSummary!, StringComparison.Ordinal);
        Assert.Contains("CARD.ULTIMATE_DEFEND", defendsSummary!, StringComparison.Ordinal);

        // SunkenTreasury.SecondChest concatenates localized strings; String.Concat must not cause a spurious gap.
        var treasury = ModelDb.Event<SunkenTreasury>();
        var secondChest = typeof(SunkenTreasury).GetMethod("SecondChest", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var (chestSummary, chestGap) = EventMechanismReader.DescribeOptionMethod(treasury, null, secondChest);
        Assert.Null(chestGap);
        Assert.Contains("CARD.GREED", chestSummary!, StringComparison.Ordinal);

        // FromDeckForTransformation excludes Quest cards (e.g. AromaOfChaos.LetGo).
        var aroma = ModelDb.Event<AromaOfChaos>();
        var letGo = typeof(AromaOfChaos).GetMethod("LetGo", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var (letGoSummary, letGoGap) = EventMechanismReader.DescribeOptionMethod(aroma, null, letGo);
        Assert.Null(letGoGap);
        Assert.Contains("transformable (removable non-Quest)", letGoSummary!, StringComparison.Ordinal);

        // SpiralingWhirlpool.ObserveTheSpiral repeats Spiral.CanEnchant and must restore its summary without gap.
        var whirlpool = ModelDb.Event<SpiralingWhirlpool>();
        var observeSpiral = typeof(SpiralingWhirlpool).GetMethod("ObserveTheSpiral", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var (spiralSummary, spiralGap) = EventMechanismReader.DescribeOptionMethod(whirlpool, null, observeSpiral);
        Assert.Null(spiralGap);
        Assert.Contains("ENCHANTMENT.SPIRAL", spiralSummary!, StringComparison.Ordinal);

        // WaterfallOfObviousSecrets.BeNice must include DingyDagger's localized base title alongside CARD.DINGY_DAGGER.
        var waterfall = ModelDb.Event<WaterfallOfObviousSecrets>();
        var beNice = typeof(WaterfallOfObviousSecrets).GetMethod("BeNice", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var (beNiceSummary, beNiceGap) = EventMechanismReader.DescribeOptionMethod(waterfall, null, beNice);
        Assert.Null(beNiceGap);
        Assert.Contains("Dingy Dagger (CARD.DINGY_DAGGER)", beNiceSummary!, StringComparison.Ordinal);

        // ZenWeaver parameterized helper RemoveCardsAndProceed must resolve 1/125 vs 2/250 distinctly.
        var weaver = ModelDb.Event<ZenWeaver>();
        var emotional = typeof(ZenWeaver).GetMethod("EmotionalAwareness", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var acupuncture = typeof(ZenWeaver).GetMethod("ArachnidAcupuncture", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var (emotionalSummary, emotionalGap) = EventMechanismReader.DescribeOptionMethod(weaver, null, emotional);
        var (acupunctureSummary, acupunctureGap) = EventMechanismReader.DescribeOptionMethod(weaver, null, acupuncture);
        Assert.Null(emotionalGap);
        Assert.Contains("select 1 removable card", emotionalSummary!, StringComparison.Ordinal);
        Assert.Contains("lose 125 Gold", emotionalSummary!, StringComparison.Ordinal);
        Assert.Null(acupunctureGap);
        Assert.Contains("select 2 removable card(s)", acupunctureSummary!, StringComparison.Ordinal);
        Assert.Contains("lose 250 Gold", acupunctureSummary!, StringComparison.Ordinal);
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
    public void Modifiers_ReportExactNativeFormulaTimingAndUnhandledHookGaps()
    {
        var (goopyText, goopyGaps) = ModifierMechanismReader.DescribeEnchantmentWithGaps(typeof(Goopy));
        Assert.Empty(goopyGaps);
        Assert.Contains("Defend-tagged cards", goopyText!, StringComparison.Ordinal);
        Assert.Contains("+(amount - 1) Block", goopyText!, StringComparison.Ordinal);
        Assert.DoesNotContain("+amount Block", goopyText!, StringComparison.Ordinal);

        var (slitherText, slitherGaps) = ModifierMechanismReader.DescribeEnchantmentWithGaps(typeof(Slither));
        Assert.Empty(slitherGaps);
        Assert.Contains("playable non-X-cost cards", slitherText!, StringComparison.Ordinal);

        var (spiralEnchText, spiralEnchGaps) = ModifierMechanismReader.DescribeEnchantmentWithGaps(typeof(Spiral));
        Assert.Empty(spiralEnchGaps);
        Assert.Contains("Basic", spiralEnchText!, StringComparison.Ordinal);
        Assert.Contains("Strike", spiralEnchText!, StringComparison.Ordinal);
        Assert.Contains("Defend", spiralEnchText!, StringComparison.Ordinal);

        var (momentumText, momentumGaps) = ModifierMechanismReader.DescribeEnchantmentWithGaps(typeof(Momentum));
        Assert.Empty(momentumGaps);
        Assert.Contains("accumulated bonus", momentumText!, StringComparison.Ordinal);
        Assert.DoesNotContain("Adds +amount", momentumText!, StringComparison.Ordinal);

        var (perfectFitText, perfectFitGaps) = ModifierMechanismReader.DescribeEnchantmentWithGaps(typeof(PerfectFit));
        Assert.Empty(perfectFitGaps);
        Assert.Contains("non-initial shuffles", perfectFitText!, StringComparison.Ordinal);

        var (emberText, emberGaps) = ModifierMechanismReader.DescribeEnchantmentWithGaps(typeof(TezcatarasEmber));
        Assert.Empty(emberGaps);
        Assert.Contains("+3 powered attack damage", emberText!, StringComparison.Ordinal);

        var (adroitText, adroitGaps) = ModifierMechanismReader.DescribeEnchantmentWithGaps(typeof(Adroit));
        Assert.Empty(adroitGaps);
        Assert.DoesNotContain("unpowered Block", adroitText!, StringComparison.Ordinal);
        Assert.Contains("scales with Block modifiers", adroitText!, StringComparison.Ordinal);

        var (slumberText, slumberGaps) = ModifierMechanismReader.DescribeEnchantmentWithGaps(typeof(SlumberingEssence));
        Assert.Empty(slumberGaps);
        Assert.Contains("until played", slumberText!, StringComparison.Ordinal);

        var (soulsText, soulsGaps) = ModifierMechanismReader.DescribeEnchantmentWithGaps(typeof(SoulsPower));
        Assert.Empty(soulsGaps);
        Assert.Contains("Removes keyword(s): Exhaust", soulsText!, StringComparison.Ordinal);

        var (glassText, glassGaps) = ModifierMechanismReader.DescribeOrbWithGaps(typeof(GlassOrb));
        Assert.Empty(glassGaps);
        Assert.Contains("minimum base 0", glassText!, StringComparison.Ordinal);
        Assert.Contains("effective Passive value is > 0", glassText!, StringComparison.Ordinal);

        // Hexed has an unhandled ClearAffliction/play hook that must surface a diagnostic gap instead of silent empty gaps.
        var (_, hexedGaps) = ModifierMechanismReader.DescribeAfflictionWithGaps(typeof(Hexed));
        Assert.NotEmpty(hexedGaps);
    }

    [Fact]
    public void PowersAndItems_ReportAccurateTargetsGuardsAndLifetimes()
    {
        var (plannerText, plannerGaps) = PowerMechanismReader.Describe(typeof(MasterPlannerPower), true);
        Assert.Empty(plannerGaps);
        Assert.Contains("apply Sly to played card", plannerText!, StringComparison.Ordinal);
        Assert.DoesNotContain("generated card", plannerText!, StringComparison.Ordinal);

        var (burrowedText, burrowedGaps) = PowerMechanismReader.Describe(typeof(BurrowedPower), true);
        Assert.Empty(burrowedGaps);
        Assert.Contains("MONSTER.TUNNELER", burrowedText!, StringComparison.Ordinal);
        Assert.DoesNotContain("BITE_MOVE", burrowedText!, StringComparison.Ordinal);

        var (thieveryText, thieveryGaps) = PowerMechanismReader.Describe(typeof(ThieveryPower), true);
        Assert.Empty(thieveryGaps);
        Assert.Contains("stores it on this power instance", thieveryText!, StringComparison.Ordinal);
        Assert.DoesNotContain("HEIST_POWER", thieveryText!, StringComparison.Ordinal);

        var (heistText, heistGaps) = PowerMechanismReader.Describe(typeof(HeistPower), true);
        Assert.Empty(heistGaps);
        Assert.Contains("target player's combat rewards", heistText!, StringComparison.Ordinal);

        var (stockText, stockGaps) = PowerMechanismReader.Describe(typeof(StockPower), true);
        Assert.Empty(stockGaps);
        Assert.Contains("MONSTER.AXEBOT", stockText!, StringComparison.Ordinal);
        Assert.Contains("Prevents combat from ending: yes", stockText!, StringComparison.Ordinal);

        var merc = ModelDb.Monster<GremlinMerc>().ToMutable();
        merc.SetUpForCombat();
        var (mercText, _) = EnemyMechanismReader.Describe(merc);
        Assert.NotNull(mercText);
        Assert.Contains("if not in Blitz mode: trigger each POWER.THIEVERY_POWER", mercText!, StringComparison.Ordinal);

        var bulletTimeRule = ItemMechanismReader.DescribeCard(ModelDb.Card<BulletTime>());
        Assert.NotNull(bulletTimeRule);
        Assert.Contains("Energy and Star costs to 0 until played or until the turn ends", bulletTimeRule!, StringComparison.Ordinal);

        var arrowStabRule = ItemMechanismReader.DescribeCard(ModelDb.Card<ArrowStab>());
        Assert.NotNull(arrowStabRule);
        Assert.Contains("owner has at least 1 arrow", arrowStabRule!, StringComparison.Ordinal);
        Assert.Contains("hand is not full", arrowStabRule!, StringComparison.Ordinal);
        Assert.Contains("Shoot card(s)", arrowStabRule!, StringComparison.Ordinal);

        var liquidRule = ItemMechanismReader.DescribePotion(ModelDb.Potion<LiquidMemories>());
        Assert.NotNull(liquidRule);
        Assert.Contains("Energy and Star costs to 0 until played or until the turn ends", liquidRule!, StringComparison.Ordinal);

        var lizardRule = ItemMechanismReader.DescribeRelic(ModelDb.Relic<LizardTail>());
        Assert.NotNull(lizardRule);
        Assert.DoesNotContain("ShouldDie", lizardRule!, StringComparison.Ordinal);
    }

    [Fact]
    public void LiveObservedIroncladAndPetPowers_ExtractCleanlyWithoutGaps()
    {
        var (stampedeText, stampedeGaps) = PowerMechanismReader.Describe(typeof(StampedePower), true);
        Assert.Empty(stampedeGaps);
        Assert.Contains("At end of turn before hand discard", stampedeText!, StringComparison.Ordinal);
        Assert.Contains("playable Attack card(s) in owner's hand", stampedeText!, StringComparison.Ordinal);
        Assert.Contains("auto-play", stampedeText!, StringComparison.Ordinal);

        var (hellraiserText, hellraiserGaps) = PowerMechanismReader.Describe(typeof(HellraiserPower), true);
        Assert.Empty(hellraiserGaps);
        Assert.Contains("early, before affliction hooks such as Bound", hellraiserText!, StringComparison.Ordinal);
        Assert.Contains("tag Strike", hellraiserText!, StringComparison.Ordinal);
        Assert.Contains("capped at 9", hellraiserText!, StringComparison.Ordinal);

        var (boulderText, boulderGaps) = PowerMechanismReader.Describe(typeof(RollingBoulderPower), true);
        Assert.Empty(boulderGaps);
        Assert.Contains("deal stacks unpowered damage to all hittable enemies", boulderText!, StringComparison.Ordinal);
        Assert.Contains("increase stacks by 5", boulderText!, StringComparison.Ordinal);

        var (freeAtkText, freeAtkGaps) = PowerMechanismReader.Describe(typeof(FreeAttackPower), true);
        Assert.Empty(freeAtkGaps);
        Assert.Contains("In-combat Energy cost (late): owned Attack cards in Hand or Play cost 0 Energy", freeAtkText!, StringComparison.Ordinal);
        Assert.Contains("Before card play:", freeAtkText!, StringComparison.Ordinal);
        Assert.Contains("subtract 1", freeAtkText!, StringComparison.Ordinal);

        var (veilText, veilGaps) = PowerMechanismReader.Describe(typeof(VeilpiercerPower), true);
        Assert.Empty(veilGaps);
        Assert.Contains("Ethereal", veilText!, StringComparison.Ordinal);
        Assert.Contains("in Hand or Play cost 0 Energy", veilText!, StringComparison.Ordinal);

        var (aggressionText, aggressionGaps) = PowerMechanismReader.Describe(typeof(AggressionPower), true);
        Assert.Empty(aggressionGaps);
        Assert.Contains("Before side turn starts:", aggressionText!, StringComparison.Ordinal);
        Assert.Contains("Attack card(s) in owner's discard pile into hand", aggressionText!, StringComparison.Ordinal);
        Assert.Contains("upgrade", aggressionText!, StringComparison.Ordinal);

        var (wuppyText, wuppyGaps) = PowerMechanismReader.Describe(typeof(WuppysLoyaltyPower), true);
        Assert.Empty(wuppyGaps);
        Assert.Contains("Damage target redirection (before Block):", wuppyText!, StringComparison.Ordinal);
        Assert.Contains("redirect damage to owner", wuppyText!, StringComparison.Ordinal);
        Assert.Contains("Targetable by hits: if event target is dead: no", wuppyText!, StringComparison.Ordinal);

        var (dieForYouText, dieForYouGaps) = PowerMechanismReader.Describe(typeof(DieForYouPower), true);
        Assert.Empty(dieForYouGaps);
        Assert.Contains("Damage target redirection (after Block, unblocked damage):", dieForYouText!, StringComparison.Ordinal);
        Assert.Contains("redirect damage to owner", dieForYouText!, StringComparison.Ordinal);

        var (jugglingText, jugglingGaps) = PowerMechanismReader.Describe(typeof(JugglingPower), true);
        Assert.Empty(jugglingGaps);
        Assert.Contains("retroactively including Attack plays already started earlier this turn", jugglingText!, StringComparison.Ordinal);
        Assert.Contains("after the 3rd Attack play resolves", jugglingText!, StringComparison.Ordinal);
        Assert.Contains("owner's hand", jugglingText!, StringComparison.Ordinal);

        // TangledPower must remain intact and VoidFormPower/EnsemblePower must continue failing closed on unrecognized cost conditions.
        var (tangledText, tangledGaps) = PowerMechanismReader.Describe(typeof(TangledPower), true);
        Assert.Empty(tangledGaps);
        Assert.Contains("cost +stacks Energy", tangledText!, StringComparison.Ordinal);

        var (_, voidFormGaps) = PowerMechanismReader.Describe(typeof(VoidFormPower), true);
        Assert.Contains("VoidFormPower.TryModifyEnergyCostInCombatLate", voidFormGaps);
    }

    [Fact]
    public void SpecialMoves_MatchNativeOrderGuardsAndPriorityPredicates()
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
        Assert.Contains("if player target has any non-Quest deck-backed card", stealLine, StringComparison.Ordinal);
        Assert.Contains("non-ENCHANTMENT.IMBUED Uncommon", stealLine, StringComparison.Ordinal);
        Assert.Contains("non-ENCHANTMENT.IMBUED Common/Rare/Event", stealLine, StringComparison.Ordinal);
        Assert.Contains("non-ENCHANTMENT.IMBUED Basic", stealLine, StringComparison.Ordinal);
        Assert.Contains("Ancient or ENCHANTMENT.IMBUED", stealLine, StringComparison.Ordinal);
        Assert.Contains("falls back to any eligible card", stealLine, StringComparison.Ordinal);
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

        var dummyEventType = typeof(EventModel).Assembly.GetTypes().Single(t => t.Name == "BattlewornDummy");
        var (dummyText, dummyGaps) = EventMechanismReader.DescribeEventTypeForProbe(dummyEventType);
        Assert.Empty(dummyGaps);
        Assert.NotNull(dummyText);
        Assert.DoesNotContain("BattlewornDummyEventV1Encounter", dummyText!, StringComparison.Ordinal);
        Assert.Contains("ENCOUNTER.BATTLEWORN_DUMMY_EVENT_V1_ENCOUNTER", dummyText!, StringComparison.Ordinal);
        Assert.Contains("ENCOUNTER.BATTLEWORN_DUMMY_EVENT_V2_ENCOUNTER", dummyText!, StringComparison.Ordinal);
        Assert.Contains("ENCOUNTER.BATTLEWORN_DUMMY_EVENT_V3_ENCOUNTER", dummyText!, StringComparison.Ordinal);

        var xRule = MacroMechanismReader.XCostRule();
        Assert.Contains("X-value modifiers", xRule, StringComparison.Ordinal);
        Assert.Contains("auto-plays spend 0 Energy", xRule, StringComparison.Ordinal);

        var ostyRule = MacroMechanismReader.OstyDamageRoutingRule();
        Assert.Contains("is alive", ostyRule, StringComparison.Ordinal);
        Assert.Contains("after the player's Block is applied", ostyRule, StringComparison.Ordinal);
        Assert.Contains("powered attack damage", ostyRule, StringComparison.Ordinal);

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
    public void RecursiveGetters_TerminateWithGapRepeatedlyOnSameReader()
    {
        var monster = new SyntheticCyclicMonster();
        var reader = new NativeRuleText(monster);
        var condMethod = typeof(SyntheticCyclicMonster).GetMethod(nameof(SyntheticCyclicMonster.CheckCyclic), BindingFlags.Instance | BindingFlags.Public)!;
        Assert.Null(reader.ReadCondition(condMethod));
        Assert.Null(reader.ReadCondition(condMethod));
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

    private sealed class SyntheticUnresolvedCallEvent : EventModel
    {
        private readonly Func<Task> _unknownAction = () => Task.CompletedTask;
        protected override IReadOnlyList<EventOption> GenerateInitialOptions() => [];

        public async Task PayThenUnknown()
        {
            await PlayerCmd.LoseGold(40m, Owner!, MegaCrit.Sts2.Core.Entities.Gold.GoldLossType.Spent);
            await _unknownAction();
        }
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
