using System.Globalization;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.Loader;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Entities.Ascension;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Powers;
using MegaCrit.Sts2.Core.Events;
using MegaCrit.Sts2.Core.Factories;
using MegaCrit.Sts2.Core.GameActions.Multiplayer;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Localization.DynamicVars;
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
        Assert.Contains("playable non-Energy-X-cost cards", slitherText!, StringComparison.Ordinal);

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
        Assert.Contains("local Energy and Star costs to 0 until played or until the turn ends", bulletTimeRule!, StringComparison.Ordinal);
        Assert.Contains("global cost modifiers still apply", bulletTimeRule!, StringComparison.Ordinal);
        Assert.Contains("Energy/Star X-cost cards ignore cost modifiers and still spend all remaining Energy/Stars when paying resources", bulletTimeRule!, StringComparison.Ordinal);

        var arrowStabRule = ItemMechanismReader.DescribeCard(ModelDb.Card<ArrowStab>());
        Assert.NotNull(arrowStabRule);
        Assert.Contains("owner has at least 1 arrow", arrowStabRule!, StringComparison.Ordinal);
        Assert.Contains("hand is not full", arrowStabRule!, StringComparison.Ordinal);
        Assert.Contains("Shoot card(s)", arrowStabRule!, StringComparison.Ordinal);

        var liquidRule = ItemMechanismReader.DescribePotion(ModelDb.Potion<LiquidMemories>());
        Assert.NotNull(liquidRule);
        Assert.Contains("local Energy and Star costs to 0 until played or until the turn ends", liquidRule!, StringComparison.Ordinal);
        Assert.Contains("global cost modifiers still apply", liquidRule!, StringComparison.Ordinal);
        Assert.Contains("Energy/Star X-cost cards ignore cost modifiers and still spend all remaining Energy/Stars when paying resources", liquidRule!, StringComparison.Ordinal);

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
        Assert.Contains("Unplayable", stampedeText!, StringComparison.Ordinal);
        Assert.Contains("Attack card(s) in owner's hand", stampedeText!, StringComparison.Ordinal);
        Assert.Contains("auto-play", stampedeText!, StringComparison.Ordinal);

        var (hellraiserText, hellraiserGaps) = PowerMechanismReader.Describe(typeof(HellraiserPower), true);
        Assert.Empty(hellraiserGaps);
        Assert.Contains("AFFLICTION.BOUND", hellraiserText!, StringComparison.Ordinal);
        Assert.Contains("tag Strike", hellraiserText!, StringComparison.Ordinal);
        Assert.Contains("capped at 9", hellraiserText!, StringComparison.Ordinal);

        var (boulderText, boulderGaps) = PowerMechanismReader.Describe(typeof(RollingBoulderPower), true);
        Assert.Empty(boulderGaps);
        Assert.Contains("deal stacks unpowered damage to all hittable enemies", boulderText!, StringComparison.Ordinal);
        Assert.Contains("increase stacks by 5", boulderText!, StringComparison.Ordinal);

        var (freeAtkText, freeAtkGaps) = PowerMechanismReader.Describe(typeof(FreeAttackPower), true);
        Assert.Empty(freeAtkGaps);
        Assert.Contains("In-combat Energy cost (late): owned Attack cards in Hand or Play cost 0 Energy (Energy X-cost cards ignore cost modifiers and still spend all remaining Energy when paying resources)", freeAtkText!, StringComparison.Ordinal);
        Assert.Contains("Before card play:", freeAtkText!, StringComparison.Ordinal);
        Assert.Contains("subtract 1", freeAtkText!, StringComparison.Ordinal);

        var (veilText, veilGaps) = PowerMechanismReader.Describe(typeof(VeilpiercerPower), true);
        Assert.Empty(veilGaps);
        Assert.Contains("Ethereal", veilText!, StringComparison.Ordinal);
        Assert.Contains("in Hand or Play cost 0 Energy (Energy X-cost cards ignore cost modifiers and still spend all remaining Energy when paying resources)", veilText!, StringComparison.Ordinal);

        var (aggressionText, aggressionGaps) = PowerMechanismReader.Describe(typeof(AggressionPower), true);
        Assert.Empty(aggressionGaps);
        Assert.Contains("Before side turn starts:", aggressionText!, StringComparison.Ordinal);
        Assert.Contains("for each of up to stacks random Attack card(s) in owner's discard pile: (move it into hand; if it is upgradable: upgrade it)", aggressionText!, StringComparison.Ordinal);

        var (wuppyText, wuppyGaps) = PowerMechanismReader.Describe(typeof(WuppysLoyaltyPower), true);
        Assert.Empty(wuppyGaps);
        Assert.Contains("Damage target redirection (before Block):", wuppyText!, StringComparison.Ordinal);
        Assert.Contains("redirect damage to owner first; any overkill continues to original target (through Block and any remaining damage redirection)", wuppyText!, StringComparison.Ordinal);
        Assert.Contains("Targetable by hits: if event target is dead: no", wuppyText!, StringComparison.Ordinal);

        var (dieForYouText, dieForYouGaps) = PowerMechanismReader.Describe(typeof(DieForYouPower), true);
        Assert.Empty(dieForYouGaps);
        Assert.Contains("Damage target redirection (after Block, unblocked damage):", dieForYouText!, StringComparison.Ordinal);
        Assert.Contains("redirect unblocked damage to owner first; any overkill continues to original target", dieForYouText!, StringComparison.Ordinal);

        var (jugglingText, jugglingGaps) = PowerMechanismReader.Describe(typeof(JugglingPower), true);
        Assert.Empty(jugglingGaps);
        Assert.Contains("retroactively including Attack plays already started earlier this turn", jugglingText!, StringComparison.Ordinal);
        Assert.Contains("after the 3rd Attack play resolves", jugglingText!, StringComparison.Ordinal);
        Assert.Contains("owner's hand", jugglingText!, StringComparison.Ordinal);

        // TangledPower must remain intact and EnsemblePower must continue failing closed on unrecognized cost conditions.
        var (tangledText, tangledGaps) = PowerMechanismReader.Describe(typeof(TangledPower), true);
        Assert.Empty(tangledGaps);
        Assert.Contains("cost +stacks Energy (Energy X-cost cards ignore cost modifiers and spend all remaining Energy when paying resources)", tangledText!, StringComparison.Ordinal);

        var (_, ensembleGaps) = PowerMechanismReader.Describe(typeof(EnsemblePower), true);
        Assert.Contains("EnsemblePower.TryModifyEnergyCostInCombatLate", ensembleGaps);
    }

    [Fact]
    public void CrossCharacterPowers_ExtractCleanlyWithoutGaps()
    {
        // Defect powers (Orb hooks, Feral, Storm, Subroutine, OneForAll, Coolant, CreativeAi, Iteration, ConsumingShadow, Loop, Thunder, TrashToTreasure)
        var (focusText, focusGaps) = PowerMechanismReader.Describe(typeof(FocusPower), true);
        Assert.Empty(focusGaps);
        Assert.Contains("POWER.FOCUS_POWER-affected orb values", focusText!, StringComparison.Ordinal);
        Assert.Contains("ORB.PLASMA_ORB is unaffected", focusText!, StringComparison.Ordinal);
        Assert.Contains("minimum 0", focusText!, StringComparison.Ordinal);

        var (loopText, loopGaps) = PowerMechanismReader.Describe(typeof(LoopPower), true);
        Assert.Empty(loopGaps);
        Assert.Contains("owner's orb count>0", loopText!, StringComparison.Ordinal);
        Assert.Contains("trigger the passive effect of owner's first (next) orb", loopText!, StringComparison.Ordinal);

        var (thunderText, thunderGaps) = PowerMechanismReader.Describe(typeof(ThunderPower), true);
        Assert.Empty(thunderGaps);
        Assert.Contains("After an orb is evoked:", thunderText!, StringComparison.Ordinal);
        Assert.Contains("ORB.LIGHTNING_ORB", thunderText!, StringComparison.Ordinal);
        Assert.Contains("living evoked targets", thunderText!, StringComparison.Ordinal);

        var (trashText, trashGaps) = PowerMechanismReader.Describe(typeof(TrashToTreasurePower), true);
        Assert.Empty(trashGaps);
        Assert.Contains("channels 1 random orb", trashText!, StringComparison.Ordinal);

        var (shadowText, shadowGaps) = PowerMechanismReader.Describe(typeof(ConsumingShadowPower), true);
        Assert.Empty(shadowGaps);
        Assert.Contains("evokes their last orb", shadowText!, StringComparison.Ordinal);

        var (coolantText, coolantGaps) = PowerMechanismReader.Describe(typeof(CoolantPower), true);
        Assert.Empty(coolantGaps);
        Assert.Contains("number of distinct orb types currently in owner's orb queue", coolantText!, StringComparison.Ordinal);

        var (creativeText, creativeGaps) = PowerMechanismReader.Describe(typeof(CreativeAiPower), true);
        Assert.Empty(creativeGaps);
        Assert.Contains("generate up to 1 distinct random combat-generatable unlocked character Power card(s)", creativeText!, StringComparison.Ordinal);

        var (feralText, feralGaps) = PowerMechanismReader.Describe(typeof(FeralPower), true);
        Assert.Empty(feralGaps);
        Assert.Contains("0-Energy Attack card plays returned by this power each turn", feralText!, StringComparison.Ordinal);
        Assert.Contains("overriding Exhaust as well", feralText!, StringComparison.Ordinal);

        var (iterText, iterGaps) = PowerMechanismReader.Describe(typeof(IterationPower), true);
        Assert.Empty(iterGaps);
        Assert.Contains("Status cards drawn by owner this turn≤1", iterText!, StringComparison.Ordinal);

        var (oneForAllText, oneForAllGaps) = PowerMechanismReader.Describe(typeof(OneForAllPower), true);
        Assert.Empty(oneForAllGaps);
        Assert.Contains("Energy spent on played card=0", oneForAllText!, StringComparison.Ordinal);
        Assert.DoesNotContain("played card is auto-played and played card is not auto-played", oneForAllText!, StringComparison.Ordinal);

        var (stormText, stormGaps) = PowerMechanismReader.Describe(typeof(StormPower), true);
        Assert.Empty(stormGaps);
        Assert.Contains("each owned Power card play", stormText!, StringComparison.Ordinal);
        Assert.Contains("ORB.LIGHTNING_ORB", stormText!, StringComparison.Ordinal);

        var (subroutineText, subroutineGaps) = PowerMechanismReader.Describe(typeof(SubroutinePower), true);
        Assert.Empty(subroutineGaps);
        Assert.Contains("each owned Power card play", subroutineText!, StringComparison.Ordinal);
        Assert.Contains("gains that much Energy", subroutineText!, StringComparison.Ordinal);

        // Regent powers (Star, Forge, EnergySpent, VoidForm, SwordSage, ForegoneConclusion, PaleBlueDot, Royalties, Tyranny)
        var (blackHoleText, blackHoleGaps) = PowerMechanismReader.Describe(typeof(BlackHolePower), true);
        Assert.Empty(blackHoleGaps);
        Assert.Contains("Stars spent on played card>0", blackHoleText!, StringComparison.Ordinal);
        Assert.Contains("After gaining Stars:", blackHoleText!, StringComparison.Ordinal);

        var (childStarsText, childStarsGaps) = PowerMechanismReader.Describe(typeof(ChildOfTheStarsPower), true);
        Assert.Empty(childStarsGaps);
        Assert.Contains("After spending Stars:", childStarsText!, StringComparison.Ordinal);
        Assert.Contains("(stacks × amount) Block", childStarsText!, StringComparison.Ordinal);

        var (furnaceText, furnaceGaps) = PowerMechanismReader.Describe(typeof(FurnacePower), true);
        Assert.Empty(furnaceGaps);
        Assert.Contains("owner forges stacks", furnaceText!, StringComparison.Ordinal);

        var (hammerText, hammerGaps) = PowerMechanismReader.Describe(typeof(HammerTimePower), true);
        Assert.Empty(hammerGaps);
        Assert.Contains("After forging:", hammerText!, StringComparison.Ordinal);
        Assert.Contains("each other living player forges amount", hammerText!, StringComparison.Ordinal);

        var (orbitText, orbitGaps) = PowerMechanismReader.Describe(typeof(OrbitPower), true);
        Assert.Empty(orbitGaps);
        Assert.Contains("every 4 Energy spent, owner gains stacks Energy", orbitText!, StringComparison.Ordinal);

        var (voidFormText, voidFormGaps) = PowerMechanismReader.Describe(typeof(VoidFormPower), true);
        Assert.Empty(voidFormGaps);
        Assert.Contains("including auto-played cards", voidFormText!, StringComparison.Ordinal);
        Assert.Contains("cost 0 Energy and 0 Stars (Energy/Star X-cost cards ignore cost modifiers and still spend all remaining Energy/Stars when paying resources)", voidFormText!, StringComparison.Ordinal);
        Assert.Contains("Application timing:", voidFormText!, StringComparison.Ordinal);

        var (swordSageText, swordSageGaps) = PowerMechanismReader.Describe(typeof(SwordSagePower), true);
        Assert.Empty(swordSageGaps);
        Assert.Contains("CARD.SOVEREIGN_BLADE", swordSageText!, StringComparison.Ordinal);
        Assert.Contains("replay 1 additional time", swordSageText!, StringComparison.Ordinal);
        Assert.DoesNotContain("BaseReplayCount", swordSageText!, StringComparison.Ordinal);

        var (foregoneText, foregoneGaps) = PowerMechanismReader.Describe(typeof(ForegoneConclusionPower), true);
        Assert.Empty(foregoneGaps);
        Assert.Contains("shuffle owner's discard pile into draw pile if draw pile is empty", foregoneText!, StringComparison.Ordinal);
        Assert.Contains("chosen card(s) from owner's draw pile into hand", foregoneText!, StringComparison.Ordinal);

        var (paleDotText, paleDotGaps) = PowerMechanismReader.Describe(typeof(PaleBlueDotPower), true);
        Assert.Empty(paleDotGaps);
        Assert.Contains("card plays finished by owner this turn≥5", paleDotText!, StringComparison.Ordinal);

        var (royaltiesText, royaltiesGaps) = PowerMechanismReader.Describe(typeof(RoyaltiesPower), true);
        Assert.Empty(royaltiesGaps);
        Assert.Contains("extra stacks Gold reward", royaltiesText!, StringComparison.Ordinal);

        var (tyrannyText, tyrannyGaps) = PowerMechanismReader.Describe(typeof(TyrannyPower), true);
        Assert.Empty(tyrannyGaps);
        Assert.Contains("Turn-start card draw:", tyrannyText!, StringComparison.Ordinal);
        Assert.Contains("exhaust stacks chosen card(s) from hand", tyrannyText!, StringComparison.Ordinal);

        // Silent, Necrobinder, Ironclad, and Colorless powers
        var (nightmareText, nightmareGaps) = PowerMechanismReader.Describe(typeof(NightmarePower), true);
        Assert.Empty(nightmareGaps);
        Assert.Contains("clone of recorded card instance", nightmareText!, StringComparison.Ordinal);

        var (phantomText, phantomGaps) = PowerMechanismReader.Describe(typeof(PhantomBladesPower), true);
        Assert.Empty(phantomGaps);
        Assert.Contains("Shiv-tagged card plays finished by owner this turn≤0", phantomText!, StringComparison.Ordinal);
        Assert.Contains("that card has Retain", phantomText!, StringComparison.Ordinal);

        var (toolsText, toolsGaps) = PowerMechanismReader.Describe(typeof(ToolsOfTheTradePower), true);
        Assert.Empty(toolsGaps);
        Assert.Contains("discard stacks chosen card(s) from hand", toolsText!, StringComparison.Ordinal);

        var (voidCallText, voidCallGaps) = PowerMechanismReader.Describe(typeof(CallOfTheVoidPower), true);
        Assert.Empty(voidCallGaps);
        Assert.Contains("combat-generatable non-Basic non-Ancient unlocked character card(s)", voidCallText!, StringComparison.Ordinal);

        var (grimoireText, grimoireGaps) = PowerMechanismReader.Describe(typeof(ForbiddenGrimoirePower), true);
        Assert.Empty(grimoireGaps);
        Assert.Contains("extra card removal reward", grimoireText!, StringComparison.Ordinal);

        var (lethalityText, lethalityGaps) = PowerMechanismReader.Describe(typeof(LethalityPower), true);
        Assert.Empty(lethalityGaps);
        Assert.Contains("at most 1 owned Attack play has started this turn", lethalityText!, StringComparison.Ordinal);

        var (sentryText, sentryGaps) = PowerMechanismReader.Describe(typeof(SentryModePower), true);
        Assert.Empty(sentryGaps);
        Assert.Contains("CARD.SWEEPING_GAZE", sentryText!, StringComparison.Ordinal);
        Assert.Contains("tags: OstyAttack", sentryText!, StringComparison.Ordinal);
        Assert.Contains("deals 10 damage to a random enemy", sentryText!, StringComparison.Ordinal);

        var (soulboundText, soulboundGaps) = PowerMechanismReader.Describe(typeof(SoulboundPower), true);
        Assert.Empty(soulboundGaps);
        Assert.Contains("CARD.SOUL", soulboundText!, StringComparison.Ordinal);
        Assert.Contains("does not re-trigger itself", soulboundText!, StringComparison.Ordinal);
        Assert.DoesNotContain("IsAddingSoul", soulboundText!, StringComparison.Ordinal);
        Assert.Contains("draw 2 cards", soulboundText!, StringComparison.Ordinal);

        var (unmovableText, unmovableGaps) = PowerMechanismReader.Describe(typeof(UnmovablePower), true);
        Assert.Empty(unmovableGaps);
        Assert.Contains("target is not a monster/pet", unmovableText!, StringComparison.Ordinal);
        Assert.Contains("earlier card/move Block gains from other card plays by owner this turn<stacks", unmovableText!, StringComparison.Ordinal);
        Assert.Contains("(source card=none or source card's owner=owner): ×2", unmovableText!, StringComparison.Ordinal);

        var (autoText, autoGaps) = PowerMechanismReader.Describe(typeof(AutomationPower), true);
        Assert.Empty(autoGaps);
        Assert.Contains("every 10 cards drawn by owner across combat", autoText!, StringComparison.Ordinal);

        var (calamityText, calamityGaps) = PowerMechanismReader.Describe(typeof(CalamityPower), true);
        Assert.Empty(calamityGaps);
        Assert.Contains("each owned Attack card play", calamityText!, StringComparison.Ordinal);
        Assert.Contains("generate stacks random combat-generatable unlocked character Attack card(s) (duplicates allowed)", calamityText!, StringComparison.Ordinal);

        var (entropyText, entropyGaps) = PowerMechanismReader.Describe(typeof(EntropyPower), true);
        Assert.Empty(entropyGaps);
        Assert.Contains("transform stacks chosen card(s) from hand into a random card", entropyText!, StringComparison.Ordinal);

        var (fastenText, fastenGaps) = PowerMechanismReader.Describe(typeof(FastenPower), true);
        Assert.Empty(fastenGaps);
        Assert.Contains("(source card=none or that card has tag Defend): +stacks", fastenText!, StringComparison.Ordinal);

        var (stratagemText, stratagemGaps) = PowerMechanismReader.Describe(typeof(StratagemPower), true);
        Assert.Empty(stratagemGaps);
        Assert.Contains("After shuffling draw pile:", stratagemText!, StringComparison.Ordinal);
        Assert.Contains("move stacks chosen card(s) from owner's draw pile into hand", stratagemText!, StringComparison.Ordinal);

        // RupturePower must not emit a duplicate generic card-play snapshot line and must distinguish resolving card plays from Burn.
        var (ruptureText, ruptureGaps) = PowerMechanismReader.Describe(typeof(RupturePower), true);
        Assert.Empty(ruptureGaps);
        Assert.Contains("Self-damage timing:", ruptureText!, StringComparison.Ordinal);
        Assert.Contains("currently resolving card play", ruptureText!, StringComparison.Ordinal);
        Assert.Contains("otherwise immediate", ruptureText!, StringComparison.Ordinal);
        Assert.DoesNotContain("Card-play power timing:", ruptureText!, StringComparison.Ordinal);
    }

    [Fact]
    public void CrossCharacterRelics_ExtractCleanlyWithoutGaps()
    {
        var relicExpectations = new (Type RelicType, string ExpectedSubstring)[]
        {
            (typeof(Brimstone), "all living opponents"),
            (typeof(RuinedHelmet), "multiply applied stacks by 2"),
            (typeof(SneckoSkull), "POWER.POISON_POWER"),
            (typeof(Tingsha), "deal 3 unpowered damage to a random hittable enemy"),
            (typeof(ToughBandages), "owner gains unpowered 3 Block"),
            (typeof(BigHat), "generate up to 2 distinct random combat-generatable unlocked character"),
            (typeof(BoneFlute), "MONSTER.OSTY"),
            (typeof(Bookmark), "local Energy cost > 0"),
            (typeof(BoundPhylactery), "After turn-start Energy reset (late):"),
            (typeof(FuneraryMask), "CARD.SOUL"),
            (typeof(IvoryTile), "played card's Energy value≥3"),
            (typeof(CrackedCore), "ORB.LIGHTNING_ORB"),
            (typeof(EmotionChip), "owner received a hit that was not fully blocked last player turn"),
            (typeof(GoldPlatedCables), "+1 additional passive trigger(s)"),
            (typeof(InfusedCore), "POWER.FOCUS_POWER-affected orb values"),
            (typeof(Metronome), "orbs channeled=7: deal 30 unpowered damage"),
            (typeof(PowerCell), "local Energy cost = 0"),
            (typeof(RunicCapacitor), "owner gains 3 orb slots"),
            (typeof(SymbioticVirus), "ORB.DARK_ORB"),
            (typeof(FencingManual), "owner forges 10"),
            (typeof(GalacticDust), "floor((stars spent ÷ 10)) × 10"),
            (typeof(MiniRegent), "After spending Stars:"),
            (typeof(VitruvianMinion), "tag Minion: ×2"),
            (typeof(CentennialPuzzle), "(used this combat = true; 3 times: owner draws 1 card)"),
            (typeof(StrikeDummy), "(attacker=owner or source card's owner=owner): +3"),
            (typeof(PhilosophersStone), "Maximum Energy:"),
            (typeof(TheAbacus), "After shuffling draw pile:"),
        };

        foreach (var (relicType, expected) in relicExpectations)
        {
            var (text, gaps) = RelicMechanismReader.Describe(relicType);
            Assert.True(gaps.Count == 0, $"{relicType.Name} had unexpected gaps: {string.Join(", ", gaps)}");
            Assert.NotNull(text);
            Assert.Contains(expected, text!, StringComparison.Ordinal);
        }
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
    public void Counterexamples_FailClosedAndPreserveSemantics()
    {
        // 1. Plain AttackCommand card (TheChariot) must not fabricate an Osty guard.
        var describeCard = typeof(NativeRuleText).GetMethod("DescribeGeneratedCard", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var reader = new NativeRuleText();
        var chariotDesc = (string?)describeCard.Invoke(reader, [typeof(TheChariot)]);
        if (chariotDesc is not null)
            Assert.DoesNotContain("OSTY", chariotDesc, StringComparison.OrdinalIgnoreCase);

        // 2. Fake VoidForm-shaped power returning false from both cost hooks must fail closed.
        var (fakeVoidText, fakeVoidGaps) = PowerMechanismReader.Describe(typeof(SyntheticNoOpVoidFormPower), true);
        Assert.Null(fakeVoidText);
        Assert.NotEmpty(fakeVoidGaps);

        // 3. OutValues must be invalidated when a by-ref parameter is passed to a mutating helper or struct .ctor.
        var readAfterClear = typeof(SyntheticIlCounterexamples).GetMethod(nameof(SyntheticIlCounterexamples.ReadAfterClear), BindingFlags.Static | BindingFlags.Public)!;
        var clearBody = NativeRuleBody.Read(readAfterClear);
        Assert.DoesNotContain(clearBody.Returns, r => r.Value is NativeRuleBody.Constant { Data: 7 });

        var readAfterStructCtor = typeof(SyntheticIlCounterexamples).GetMethod(nameof(SyntheticIlCounterexamples.ReadAfterStructCtor), BindingFlags.Static | BindingFlags.Public)!;
        var ctorBody = NativeRuleBody.Read(readAfterStructCtor);
        Assert.DoesNotContain(ctorBody.Returns, r => r.Value is NativeRuleBody.Constant { Data: 7 });

        // 4. Dictionary.Remove out-value loop in generic ReadEffects must fail closed with a loop-count gap.
        var (dictLoopText, dictLoopGaps) = PowerMechanismReader.Describe(typeof(SyntheticDictRemovedLoopPower), true);
        Assert.Null(dictLoopText);
        Assert.Contains("AfterCardPlayed/loop-count", dictLoopGaps);

        // 5. Non-zero-start decimal loop (i = 2; (decimal)i < dv.BaseValue; i++) must not record Count = dv.BaseValue.
        var nonZeroDecLoop = typeof(SyntheticIlCounterexamples).GetMethod(nameof(SyntheticIlCounterexamples.LoopStartingAtTwo), BindingFlags.Static | BindingFlags.Public)!;
        var loopBody = NativeRuleBody.Read(nonZeroDecLoop);
        Assert.True(loopBody.Incomplete);
        Assert.Empty(loopBody.Loops);

        // 6. Struct .ctor with writable ref must not push a bogus return value on the stack.
        var boolAfterCtor = typeof(SyntheticIlCounterexamples).GetMethod(nameof(SyntheticIlCounterexamples.ReturnFalseAfterStructCtor), BindingFlags.Static | BindingFlags.Public)!;
        var boolCtorBody = NativeRuleBody.Read(boolAfterCtor);
        Assert.All(boolCtorBody.Returns, r => Assert.Equal(0, (r.Value as NativeRuleBody.Constant)?.Data));

        // 7. Nested Dictionary.Remove loop inside an outer unknown loop must still fail closed.
        var (nestedLoopText, nestedLoopGaps) = PowerMechanismReader.Describe(typeof(SyntheticNestedDictLoopPower), true);
        Assert.Null(nestedLoopText);
        Assert.Contains("AfterCardPlayed/loop-count", nestedLoopGaps);

        // 8. Do-while decimal loop and counter-dependent decimal loop must not collapse into fixed-count loops.
        var doWhileDec = typeof(SyntheticIlCounterexamples).GetMethod(nameof(SyntheticIlCounterexamples.DoWhileDecimalLoop), BindingFlags.Static | BindingFlags.Public)!;
        Assert.Empty(NativeRuleBody.Read(doWhileDec).Loops);
        var counterDepDec = typeof(SyntheticIlCounterexamples).GetMethod(nameof(SyntheticIlCounterexamples.CounterDependentDecimalLoop), BindingFlags.Static | BindingFlags.Public)!;
        Assert.Empty(NativeRuleBody.Read(counterDepDec).Loops);

        // 9. Cross-hook boolean flag must not be folded as a pure single-method re-entrancy lock.
        var crossHookText = new NativeRuleText();
        var crossLines = new List<string>();
        crossHookText.ReadHooks(typeof(SyntheticCrossHookGuardPower), crossLines);
        Assert.DoesNotContain(crossLines, l => l.Contains("does not re-trigger itself", StringComparison.Ordinal));

        // 10. PersonalHivePower (single loop reached via two Osty branches) must stay `stacks times:`, never `(stacks × stacks) times:`.
        var (hiveText, hiveGaps) = PowerMechanismReader.Describe(typeof(PersonalHivePower), true);
        Assert.Empty(hiveGaps);
        Assert.Contains("stacks times:", hiveText!, StringComparison.Ordinal);
        Assert.DoesNotContain("stacks × stacks", hiveText!, StringComparison.Ordinal);

        // 11. ReboundPower, NostalgiaPower, VelvetChoker, ShrinkPower, CharonsAshes, LostWisp, BufferPower, JossPaper, CacophonyPower, ThrowingAxe.
        var (reboundText, reboundGaps) = PowerMechanismReader.Describe(typeof(ReboundPower), true);
        Assert.Empty(reboundGaps);
        Assert.Contains("move to draw pile (top) instead of discard pile, consuming 1", reboundText!, StringComparison.Ordinal);
        Assert.Contains("when play starts (before card effects/nested plays resolve)", reboundText!, StringComparison.Ordinal);

        var (nostalgiaText, nostalgiaGaps) = PowerMechanismReader.Describe(typeof(NostalgiaPower), true);
        Assert.Empty(nostalgiaGaps);
        Assert.Contains("owner's first N Attack or Skill plays each turn (where N = stacks) move to draw pile (top) instead of discard pile", nostalgiaText!, StringComparison.Ordinal);

        var (chokerText, chokerGaps) = RelicMechanismReader.Describe(typeof(VelvetChoker));
        Assert.Empty(chokerGaps);
        Assert.Contains("card plays started by owner this turn≥6: no", chokerText!, StringComparison.Ordinal);
        Assert.DoesNotContain("ShouldPreventCardPlay", chokerText!, StringComparison.Ordinal);

        var (shrinkText, shrinkGaps) = PowerMechanismReader.Describe(typeof(ShrinkPower), true);
        Assert.Empty(shrinkGaps);
        Assert.Contains("stacks≥0", shrinkText!, StringComparison.Ordinal);
        Assert.DoesNotContain("IsInfinite", shrinkText!, StringComparison.Ordinal);

        var (ashesText, ashesGaps) = RelicMechanismReader.Describe(typeof(CharonsAshes));
        Assert.Empty(ashesGaps);
        Assert.Contains("deal 3 unpowered damage", ashesText!, StringComparison.Ordinal);

        var (wispText, wispGaps) = RelicMechanismReader.Describe(typeof(MegaCrit.Sts2.Core.Models.Relics.LostWisp));
        Assert.Empty(wispGaps);
        Assert.Contains("deal 8 unpowered damage", wispText!, StringComparison.Ordinal);

        var (bufferText, bufferGaps) = PowerMechanismReader.Describe(typeof(BufferPower), true);
        Assert.Empty(bufferGaps);
        Assert.Contains("CARD.BRIGHTEST_FLAME", bufferText!, StringComparison.Ordinal);

        var (jossText, jossGaps) = RelicMechanismReader.Describe(typeof(JossPaper));
        Assert.Empty(jossGaps);
        Assert.Contains("owner draws (previous cards exhausted ÷ 5) cards", jossText!, StringComparison.Ordinal);
        Assert.DoesNotContain("previous previous", jossText!, StringComparison.Ordinal);

        var (cacophonyText, cacophonyGaps) = PowerMechanismReader.Describe(typeof(CacophonyPower), true);
        Assert.Empty(cacophonyGaps);
        Assert.Contains("if counter cards≤0: (set counter cards to 33; if a random hittable enemy≠none: deal stacks unpowered damage", cacophonyText!, StringComparison.Ordinal);

        var (_, throwingAxeGaps) = RelicMechanismReader.Describe(typeof(ThrowingAxe));
        Assert.Contains("ThrowingAxe.ModifyCardPlayCount", throwingAxeGaps);

        var spiralText = ModifierMechanismReader.DescribeEnchantment(typeof(Spiral));
        Assert.NotNull(spiralText);
        Assert.Contains("Basic cards tagged Defend or Strike", spiralText!, StringComparison.Ordinal);

        // 12. Array element by-ref clear (ldelema) & async lifted parameter by-ref clear must invalidate stale values.
        var arrClearMethod = typeof(SyntheticIlCounterexamples).GetMethod(nameof(SyntheticIlCounterexamples.ArrayElementByRefClear), BindingFlags.Static | BindingFlags.Public)!;
        var arrClearBody = NativeRuleBody.Read(arrClearMethod);
        Assert.DoesNotContain(arrClearBody.Returns, r => r.Value is NativeRuleBody.Constant { Data: 7 });

        var (asyncParamText, asyncParamGaps) = PowerMechanismReader.Describe(typeof(SyntheticAsyncParamClearPower), true);
        Assert.Null(asyncParamText);
        Assert.NotEmpty(asyncParamGaps);

        // 13. Decimal loop mutating another local (`reward++`) must not collapse into fixed-count repetition.
        var mutatingLoopMethod = typeof(SyntheticIlCounterexamples).GetMethod(nameof(SyntheticIlCounterexamples.DecimalLoopMutatingOtherLocal), BindingFlags.Static | BindingFlags.Public)!;
        Assert.Empty(NativeRuleBody.Read(mutatingLoopMethod).Loops);

        // 14. Constructor-set guard flag must not fold into `does not re-trigger itself`.
        var ctorGuardReader = new NativeRuleText();
        var ctorGuardLines = new List<string>();
        ctorGuardReader.ReadHooks(typeof(SyntheticCtorGuardPower), ctorGuardLines);
        Assert.DoesNotContain(ctorGuardLines, l => l.Contains("does not re-trigger itself", StringComparison.Ordinal));

        // 15. Cost hook restoring originalCost on active path or using standalone `Pile != X` must fail closed.
        var (restoredCostText, restoredCostGaps) = PowerMechanismReader.Describe(typeof(SyntheticRestoredCostPower), true);
        Assert.Null(restoredCostText);
        Assert.NotEmpty(restoredCostGaps);

        var (negPileText, negPileGaps) = PowerMechanismReader.Describe(typeof(SyntheticStandaloneNegatedPileCostPower), true);
        Assert.Null(negPileText);
        Assert.NotEmpty(negPileGaps);

        // 16. Mixed OR enchantment eligibility (`Basic || Strike`) must render disjunction, never `Basic Strike-tagged`.
        var mixedOrMethod = typeof(SyntheticIlCounterexamples).GetMethod(nameof(SyntheticIlCounterexamples.MixedOrCanEnchant), BindingFlags.Static | BindingFlags.Public)!;
        var describeCustom = typeof(ModifierMechanismReader).GetMethod("DescribeCustomCanEnchant", BindingFlags.Static | BindingFlags.NonPublic)!;
        var mixedOrDesc = (string?)describeCustom.Invoke(null, [NativeRuleBody.Read(mixedOrMethod)]);
        Assert.NotNull(mixedOrDesc);
        Assert.Contains("Basic cards", mixedOrDesc!, StringComparison.Ordinal);
        Assert.Contains("Strike-tagged cards", mixedOrDesc!, StringComparison.Ordinal);
        Assert.Contains(" or ", mixedOrDesc!, StringComparison.Ordinal);
        Assert.DoesNotContain("Basic Strike-tagged", mixedOrDesc!, StringComparison.Ordinal);

        // 17. Separate foreach loops over independent Take(Amount) samples must not be merged into one loop.
        var (sepLoopsText, _) = PowerMechanismReader.Describe(typeof(SyntheticSeparateForeachPower), true);
        Assert.NotNull(sepLoopsText);
        Assert.DoesNotContain("for each of ", sepLoopsText!, StringComparison.Ordinal);

        // 18. AutoPlayFromDrawPile with forceExhaust: true (Havoc) must mention Exhaust; JackOfAllTrades filter must fail closed.
        var readEffects = typeof(NativeRuleText).GetMethod("ReadEffects", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var havocMethod = typeof(Havoc).GetMethod("OnPlay", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var havocReader = new NativeRuleText();
        var havocRes = readEffects.Invoke(havocReader, [havocMethod, null, 0, false, null])!;
        var havocEffectsObj = (System.Collections.IEnumerable)havocRes.GetType().GetField("Item1")!.GetValue(havocRes)!;
        var havocIncomplete = (bool)havocRes.GetType().GetField("Item2")!.GetValue(havocRes)!;
        Assert.False(havocIncomplete);
        var havocTexts = havocEffectsObj.Cast<object>().Select(e => (string)e.GetType().GetProperty("Text")!.GetValue(e)!).ToArray();
        Assert.Contains(havocTexts, t => t.Contains("ExhaustOnNextPlay", StringComparison.Ordinal) && t.Contains("non-Power", StringComparison.Ordinal));

        var joatMethod = typeof(JackOfAllTrades).GetMethod("OnPlay", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var joatReader = new NativeRuleText();
        var joatRes = readEffects.Invoke(joatReader, [joatMethod, null, 0, false, null])!;
        var joatIncomplete = (bool)joatRes.GetType().GetField("Item2")!.GetValue(joatRes)!;
        Assert.True(joatIncomplete);

        // 19. EchoFormPower/BurstPower do not exclude dupes; MonologuePower/DoomPower/PoisonPower reflect exact native semantics.
        var (echoText, echoGaps) = PowerMechanismReader.Describe(typeof(EchoFormPower), true);
        Assert.Empty(echoGaps);
        Assert.DoesNotContain("non-dupe", echoText!, StringComparison.Ordinal);

        var (monologueText, monologueGaps) = PowerMechanismReader.Describe(typeof(MonologuePower), true);
        Assert.Empty(monologueGaps);
        Assert.Contains("snapshots 1 before each owned card play starts", monologueText!, StringComparison.Ordinal);
        Assert.Contains("add 1 to counter strength applied", monologueText!, StringComparison.Ordinal);

        var (doomText, doomGaps) = PowerMechanismReader.Describe(typeof(DoomPower), true);
        Assert.Empty(doomGaps);
        Assert.Contains("before enemy turn ends", doomText!, StringComparison.Ordinal);
        Assert.Contains("after player turn ends", doomText!, StringComparison.Ordinal);

        var (poisonText, poisonGaps) = PowerMechanismReader.Describe(typeof(PoisonPower), true);
        Assert.Empty(poisonGaps);
        Assert.Contains("min(stacks, 1 + total opposing", poisonText!, StringComparison.Ordinal);

        // 20. Counted loop exit invalidates loop counter; sync parameter ref clear invalidates stale parameter;
        // foreach without reading element preserves per-item repetition.
        var postLoopMethod = typeof(SyntheticIlCounterexamples).GetMethod(nameof(SyntheticIlCounterexamples.ReadCounterAfterLoop), BindingFlags.Static | BindingFlags.Public)!;
        var postLoopBody = NativeRuleBody.Read(postLoopMethod);
        Assert.DoesNotContain(postLoopBody.Returns, r => r.Value is NativeRuleBody.Constant { Data: 1 });

        var (syncParamText, syncParamGaps) = PowerMechanismReader.Describe(typeof(SyntheticSyncParamClearPower), true);
        Assert.Null(syncParamText);
        Assert.NotEmpty(syncParamGaps);

        var (foreachNonItemText, foreachNonItemGaps) = PowerMechanismReader.Describe(typeof(SyntheticForeachNonItemPower), true);
        Assert.Empty(foreachNonItemGaps);
        Assert.Contains("for each item in owner's hand: owner gains 1 Energy", foreachNonItemText!, StringComparison.Ordinal);

        // 21. Cost hook ref-helper restore, non-constant return, and multi-keyword preservation.
        var (refRestoreText, refRestoreGaps) = PowerMechanismReader.Describe(typeof(SyntheticRefRestoreCostPower), true);
        Assert.Null(refRestoreText);
        Assert.NotEmpty(refRestoreGaps);

        var (nonConstRetText, nonConstRetGaps) = PowerMechanismReader.Describe(typeof(SyntheticNonConstReturnCostPower), true);
        Assert.Null(nonConstRetText);
        Assert.NotEmpty(nonConstRetGaps);

        var (multiKwText, multiKwGaps) = PowerMechanismReader.Describe(typeof(SyntheticMultiKeywordCostPower), true);
        Assert.Empty(multiKwGaps);
        Assert.Contains("Retain", multiKwText!, StringComparison.Ordinal);
        Assert.Contains("Exhaust", multiKwText!, StringComparison.Ordinal);

        // 22. Inverted GainsBlock enchantment and conditional EnchantPlayCount must fail closed.
        var negBlockMethod = typeof(SyntheticIlCounterexamples).GetMethod(nameof(SyntheticIlCounterexamples.NegatedGainsBlock), BindingFlags.Static | BindingFlags.Public)!;
        var isPosBlock = typeof(ModifierMechanismReader).GetMethod("IsPositiveGainsBlockCanEnchant", BindingFlags.Static | BindingFlags.NonPublic)!;
        Assert.False((bool)isPosBlock.Invoke(null, [NativeRuleBody.Read(negBlockMethod)])!);

        var condReplayMethod = typeof(SyntheticIlCounterexamples).GetMethod(nameof(SyntheticIlCounterexamples.ConditionalEnchantPlayCount), BindingFlags.Static | BindingFlags.Public)!;
        var resolveDelta = typeof(ModifierMechanismReader).GetMethod("ResolveEnchantPlayCountDelta", BindingFlags.Static | BindingFlags.NonPublic)!;
        Assert.Null(resolveDelta.Invoke(null, [typeof(Spiral), NativeRuleBody.Read(condReplayMethod)]));

        // 23. Event helper self-named parameter binding (`Pay(amount) -> Give(amount)`) must terminate cleanly.
        var selfBindEvent = new SyntheticSelfBindEvent();
        var payMethod = typeof(SyntheticSelfBindEvent).GetMethod(nameof(SyntheticSelfBindEvent.Pay), BindingFlags.Instance | BindingFlags.Public)!;
        var (_, selfBindGap) = EventMechanismReader.DescribeOptionMethod(selfBindEvent, null, payMethod);
        Assert.Null(selfBindGap);

        // 24. Constant GroupBy key selector (`group orb by 0`) must fail closed instead of claiming distinct orb types.
        var (constGroupText, constGroupGaps) = PowerMechanismReader.Describe(typeof(SyntheticConstGroupCoolantPower), true);
        Assert.Null(constGroupText);
        Assert.NotEmpty(constGroupGaps);

        // 25. Native gameplay accuracy: AimPower (FromArrow + HeavyShot), SuckPower (multi-hit), OstyCmd.Summon, BrilliantScarf X-cost, Havoc default-Exhaust.
        var (aimText, aimGaps) = PowerMechanismReader.Describe(typeof(AimPower), true);
        Assert.Empty(aimGaps);
        Assert.Contains("ValueProp.FromArrow", aimText!, StringComparison.Ordinal);
        Assert.Contains("CARD.HEAVY_SHOT", aimText!, StringComparison.Ordinal);

        var (suckText, suckGaps) = PowerMechanismReader.Describe(typeof(SuckPower), true);
        Assert.Empty(suckGaps);
        Assert.Contains("stacks × qualifying hits", suckText!, StringComparison.Ordinal);

        var (phylacteryText, phylacteryGaps) = RelicMechanismReader.Describe(typeof(PhylacteryUnbound));
        Assert.Empty(phylacteryGaps);
        Assert.Contains("or if already alive, increases its max HP and heals it by 2", phylacteryText!, StringComparison.Ordinal);

        var scarfText = ItemMechanismReader.ReadRelic(ModelDb.Relic<BrilliantScarf>(), "eng");
        Assert.NotNull(scarfText);
        Assert.Contains("X-cost cards ignore cost modifiers", scarfText!, StringComparison.Ordinal);
        Assert.Contains(havocTexts, t => t.Contains("default to", StringComparison.Ordinal) && t.Contains("unless overridden by a destination modifier", StringComparison.Ordinal));

        // 26. Sync ref-local alias write (`ref int alias = ref amount; alias = 0`) must invalidate `amount` and fail closed.
        var (aliasText, aliasGaps) = PowerMechanismReader.Describe(typeof(SyntheticRefAliasPower), true);
        Assert.Null(aliasText);
        Assert.NotEmpty(aliasGaps);

        // 27. Nested foreach loops must preserve BOTH outer and inner repetition prefixes.
        var (nestedFeText, nestedFeGaps) = PowerMechanismReader.Describe(typeof(SyntheticNestedForeachPower), true);
        Assert.Empty(nestedFeGaps);
        Assert.Contains("for each item in owner's hand: for each item in owner's discard pile: owner gains 1 Energy", nestedFeText!, StringComparison.Ordinal);

        // 28. CardFactory followed by `.Select(...)` projection must fail closed instead of claiming the original pool.
        var (selectProjText, selectProjGaps) = PowerMechanismReader.Describe(typeof(SyntheticCardFactorySelectPower), true);
        Assert.Null(selectProjText);
        Assert.NotEmpty(selectProjGaps);

        // 29. Event helper shadowed parameter (`Pay(int amount) => Give(9, amount)`) must not re-bind caller's `amount` to callee's `9`.
        var shadowEvent = new SyntheticShadowBindEvent();
        var shadowPay = typeof(SyntheticShadowBindEvent).GetMethod(nameof(SyntheticShadowBindEvent.Pay), BindingFlags.Instance | BindingFlags.Public)!;
        var (shadowDesc, shadowGap) = EventMechanismReader.DescribeOptionMethod(shadowEvent, null, shadowPay);
        Assert.Null(shadowGap);
        Assert.DoesNotContain("9", shadowDesc ?? "", StringComparison.Ordinal);

        // 30. SoulsPower keyword source (`KeywordSources.None`) and dual-keyword `CanEnchant` must fail closed.
        var tryReadLocalKw = typeof(ModifierMechanismReader).GetMethod("TryReadLocalKeywordCanEnchant", BindingFlags.Static | BindingFlags.NonPublic)!;
        var noneSrcMethod = typeof(SyntheticIlCounterexamples).GetMethod(nameof(SyntheticIlCounterexamples.CanEnchantNoneSourceExhaust), BindingFlags.Static | BindingFlags.Public)!;
        var dualKwMethod = typeof(SyntheticIlCounterexamples).GetMethod(nameof(SyntheticIlCounterexamples.CanEnchantRetainAndExhaust), BindingFlags.Static | BindingFlags.Public)!;
        Assert.Null(tryReadLocalKw.Invoke(null, [NativeRuleBody.Read(noneSrcMethod)]));
        Assert.Null(tryReadLocalKw.Invoke(null, [NativeRuleBody.Read(dualKwMethod)]));

        // 31. Combined first-N + CardType replay preserves CardType; standalone `attack.WithHitCount(3)` and mixed card cost mutations fail closed.
        var (typedEchoText, typedEchoGaps) = PowerMechanismReader.Describe(typeof(SyntheticTypedEchoFormPower), true);
        Assert.Empty(typedEchoGaps);
        Assert.Contains("Attack", typedEchoText!, StringComparison.Ordinal);

        var (mutatedAttackCardText, _) = PowerMechanismReader.Describe(typeof(SyntheticStandaloneAttackMutatorPower), true);
        Assert.DoesNotContain("deal 25 damage to all enemies", mutatedAttackCardText ?? "", StringComparison.Ordinal);

        var describeCardCost = typeof(ItemMechanismReader).GetMethod("DescribeCardCostLifetime", BindingFlags.Static | BindingFlags.NonPublic)!;
        var mixedCostMethod = typeof(SyntheticIlCounterexamples).GetMethod(nameof(SyntheticIlCounterexamples.SetFreeThenSetNine), BindingFlags.Static | BindingFlags.Public)!;
        Assert.Null(describeCardCost.Invoke(null, [NativeRuleBody.Read(mixedCostMethod)]));

        // 32. Mutated specialized power variants must never emit the unmutated specialized template.
        foreach (var mutatedPower in new[]
        {
            typeof(SyntheticMutatedAutomationPower),
            typeof(SyntheticMutatedOrbitPower),
            typeof(SyntheticMutatedVoidFormPower),
            typeof(SyntheticMutatedImitationPower),
            typeof(SyntheticMutatedHellraiserPower),
            typeof(SyntheticMutatedJugglingPower),
        })
        {
            var (mutText, mutGaps) = PowerMechanismReader.Describe(mutatedPower, true);
            Assert.NotEmpty(mutGaps);
            Assert.Null(mutText);
        }
        var (mutPoisonText, mutPoisonGaps) = PowerMechanismReader.Describe(typeof(SyntheticMutatedPoisonPower), true);
        Assert.Empty(mutPoisonGaps);
        Assert.Contains("1 times:", mutPoisonText!, StringComparison.Ordinal);
        Assert.DoesNotContain("ACCELERANT", mutPoisonText!, StringComparison.Ordinal);
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

    private sealed class SyntheticNoOpVoidFormPower : PowerModel
    {
        private sealed class Data { public int count; }
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Counter;
        protected override object InitInternalData() => new Data();
        public override bool TryModifyEnergyCostInCombatLate(CardModel card, decimal originalCost, out decimal modifiedCost)
        {
            modifiedCost = originalCost;
            return false;
        }
        public override bool TryModifyStarCost(CardModel card, decimal originalCost, out decimal modifiedCost)
        {
            modifiedCost = originalCost;
            return false;
        }
        public override Task AfterCardPlayed(PlayerChoiceContext choiceContext, CardPlay cardPlay)
        {
            GetInternalData<Data>().count++;
            return Task.CompletedTask;
        }
        public override Task BeforeSideTurnStart(PlayerChoiceContext choiceContext, CombatSide side, IReadOnlyList<Creature> participants, ICombatState combatState)
        {
            GetInternalData<Data>().count = 0;
            return Task.CompletedTask;
        }
    }

    private sealed class SyntheticDictRemovedLoopPower : PowerModel
    {
        private sealed class Data { public readonly Dictionary<CardModel, int> map = []; }
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Counter;
        protected override object InitInternalData() => new Data();
        public override async Task AfterCardPlayed(PlayerChoiceContext choiceContext, CardPlay cardPlay)
        {
            if (GetInternalData<Data>().map.Remove(cardPlay.Card, out var n))
            {
                for (int i = 0; i < n; i++)
                    await PlayerCmd.GainEnergy(1m, Owner.Player!);
            }
        }
    }

    private static class SyntheticIlCounterexamples
    {
        private struct MutatingStruct
        {
            public int Value;
            public MutatingStruct(ref int x)
            {
                x = 11;
                Value = x;
            }
        }

        private static void ClearRef(ref int x) => x = 0;

        public static int ReadAfterClear(out int x)
        {
            x = 7;
            ClearRef(ref x);
            return x;
        }

        public static int ReadAfterStructCtor(out int x)
        {
            x = 7;
            var s = new MutatingStruct(ref x);
            return x + s.Value - s.Value;
        }

        public static int LoopStartingAtTwo(MegaCrit.Sts2.Core.Localization.DynamicVars.DynamicVar dv)
        {
            int hits = 0;
            for (int i = 2; (decimal)i < dv.BaseValue; i++)
                hits++;
            return hits;
        }

        public static bool ReturnFalseAfterStructCtor(out int x)
        {
            x = 7;
            bool result = false;
            var s = new MutatingStruct(ref x);
            _ = s.Value;
            return result;
        }

        public static int DoWhileDecimalLoop(MegaCrit.Sts2.Core.Localization.DynamicVars.DynamicVar dv)
        {
            int hits = 0;
            int i = 0;
            do
            {
                hits++;
                i++;
            } while ((decimal)i < dv.BaseValue);
            return hits;
        }

        public static int CounterDependentDecimalLoop(MegaCrit.Sts2.Core.Localization.DynamicVars.DynamicVar dv)
        {
            int sum = 0;
            for (int i = 0; (decimal)i < dv.BaseValue; i++)
                sum += i + 1;
            return sum;
        }

        public static int ArrayElementByRefClear()
        {
            var a = new int[1];
            a[0] = 7;
            ClearRef(ref a[0]);
            return a[0];
        }

        public static int DecimalLoopMutatingOtherLocal(MegaCrit.Sts2.Core.Localization.DynamicVars.DynamicVar dv)
        {
            int reward = 0;
            int total = 0;
            for (int i = 0; (decimal)i < dv.BaseValue; i++)
            {
                reward++;
                total += reward;
            }
            return total;
        }

        public static bool MixedOrCanEnchant(CardModel card)
            => card.Rarity == CardRarity.Basic || card.Tags.Contains(CardTag.Strike);

        public static int ReadCounterAfterLoop()
        {
            int i;
            for (i = 0; i < 3; i++)
                _ = i.GetHashCode();
            return i;
        }

        public static bool NegatedGainsBlock(CardModel card) => !card.GainsBlock;

        public static int ConditionalEnchantPlayCount(CardModel card, int playCount)
            => card.IsUpgraded ? playCount + 3 : playCount + 1;

        public static bool CanEnchantNoneSourceExhaust(CardModel card)
            => card.GetKeywordsWithSources(KeywordSources.None).Contains(CardKeyword.Exhaust);

        public static bool CanEnchantRetainAndExhaust(CardModel card)
            => card.GetKeywordsWithSources(KeywordSources.Local).Contains(CardKeyword.Retain)
                && card.GetKeywordsWithSources(KeywordSources.Local).Contains(CardKeyword.Exhaust);

        public static void SetFreeThenSetNine(CardModel card)
        {
            card.SetToFreeThisTurn();
            card.EnergyCost.SetThisTurn(9);
        }
    }

    private sealed class SyntheticSyncParamClearPower : PowerModel
    {
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Single;
        private static void ZeroInt(ref int x) => x = 0;
        public override Task AfterEnergySpent(CardModel card, int amount)
        {
            ZeroInt(ref amount);
            return PlayerCmd.GainEnergy(amount, Owner.Player!);
        }
    }

    private sealed class SyntheticForeachNonItemPower : PowerModel
    {
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Single;
        public override async Task AfterSideTurnStart(CombatSide side, IReadOnlyList<Creature> participants, ICombatState combatState)
        {
            if (!participants.Contains(Owner))
                return;
            foreach (var _ in PileType.Hand.GetPile(Owner.Player!).Cards)
                await PlayerCmd.GainEnergy(1m, Owner.Player!);
        }
    }

    private sealed class SyntheticRefRestoreCostPower : PowerModel
    {
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Single;
        private static void Restore(ref decimal target, decimal original) => target = original;
        public override bool TryModifyEnergyCostInCombatLate(CardModel card, decimal originalCost, out decimal modifiedCost)
        {
            modifiedCost = 0m;
            if (card.Owner.Creature != Owner)
                return false;
            Restore(ref modifiedCost, originalCost);
            return true;
        }
    }

    private sealed class SyntheticNonConstReturnCostPower : PowerModel
    {
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Single;
        public override bool TryModifyEnergyCostInCombatLate(CardModel card, decimal originalCost, out decimal modifiedCost)
        {
            if (card.Owner.Creature != Owner)
            {
                modifiedCost = originalCost;
                return false;
            }
            if (card.Type == CardType.Skill)
            {
                modifiedCost = 0m;
                return true;
            }
            modifiedCost = 9m;
            return card.IsUpgradable;
        }
    }

    private sealed class SyntheticMultiKeywordCostPower : PowerModel
    {
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Single;
        public override bool TryModifyEnergyCostInCombatLate(CardModel card, decimal originalCost, out decimal modifiedCost)
        {
            if (card.Owner.Creature != Owner || !card.Keywords.Contains(CardKeyword.Retain) || !card.Keywords.Contains(CardKeyword.Exhaust))
            {
                modifiedCost = originalCost;
                return false;
            }
            modifiedCost = 0m;
            return true;
        }
    }

    private sealed class SyntheticConstGroupCoolantPower : PowerModel
    {
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Counter;
        public override async Task AfterSideTurnStart(CombatSide side, IReadOnlyList<Creature> participants, ICombatState combatState)
        {
            if (participants.Contains(Owner))
            {
                var num = (from orb in Owner.Player!.PlayerCombatState!.OrbQueue.Orbs group orb by 0).Count();
                await CreatureCmd.GainBlock(Owner, num * Amount, ValueProp.Unpowered, null);
            }
        }
    }

    private sealed class SyntheticSelfBindEvent : EventModel
    {
        protected override IReadOnlyList<EventOption> GenerateInitialOptions() => [];
        public Task Pay(int amount) => Give(amount);
        private Task Give(int amount) => PlayerCmd.GainGold(amount, Owner!);
    }

    private sealed class SyntheticAsyncParamClearPower : PowerModel
    {
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Single;
        private static void ZeroInt(ref int x) => x = 0;
        public override async Task AfterEnergySpent(CardModel card, int amount)
        {
            await Task.Yield();
            ZeroInt(ref amount);
            await PlayerCmd.GainEnergy(amount, Owner.Player!);
        }
    }

    private sealed class SyntheticCtorGuardPower : PowerModel
    {
        private bool _guard;
        public SyntheticCtorGuardPower() { _guard = true; }
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Single;
        private bool GuardFlag { get => _guard; set => _guard = value; }
        public override async Task AfterDamageReceived(PlayerChoiceContext choiceContext, Creature target, DamageResult result, ValueProp props, Creature? dealer, CardModel? cardSource)
        {
            if (target == Owner && !GuardFlag)
            {
                GuardFlag = true;
                await PlayerCmd.GainEnergy(1m, Owner.Player!);
                GuardFlag = false;
            }
        }
    }

    private sealed class SyntheticRestoredCostPower : PowerModel
    {
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Single;
        public override bool TryModifyEnergyCostInCombatLate(CardModel card, decimal originalCost, out decimal modifiedCost)
        {
            modifiedCost = 0m;
            if (card.Owner.Creature != Owner)
                return false;
            modifiedCost = originalCost;
            return true;
        }
    }

    private sealed class SyntheticStandaloneNegatedPileCostPower : PowerModel
    {
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Single;
        public override bool TryModifyEnergyCostInCombatLate(CardModel card, decimal originalCost, out decimal modifiedCost)
        {
            modifiedCost = originalCost;
            if (card.Owner.Creature != Owner || card.Pile?.Type == PileType.Exhaust)
                return false;
            modifiedCost = 0m;
            return true;
        }
    }

    private sealed class SyntheticSeparateForeachPower : PowerModel
    {
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Counter;
        public override async Task BeforeSideTurnStart(PlayerChoiceContext choiceContext, CombatSide side, IReadOnlyList<Creature> participants, ICombatState combatState)
        {
            if (!participants.Contains(Owner))
                return;
            var pile = PileType.Discard.GetPile(Owner.Player!);
            foreach (var c in pile.Cards.Where(c => c.Type == CardType.Attack).ToList().Take(Amount))
                await CardPileCmd.Add(c, PileType.Hand);
            foreach (var c in pile.Cards.Where(c => c.Type == CardType.Attack).ToList().Take(Amount))
                if (c.IsUpgradable) CardCmd.Upgrade(c);
        }
    }

    private sealed class SyntheticNestedDictLoopPower : PowerModel
    {
        private sealed class Data { public readonly Dictionary<CardModel, int> map = []; }
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Counter;
        protected override object InitInternalData() => new Data();
        public override async Task AfterCardPlayed(PlayerChoiceContext choiceContext, CardPlay cardPlay)
        {
            if (GetInternalData<Data>().map.Remove(cardPlay.Card, out var n))
            {
                for (int i = 0; i < n; i++)
                {
                    for (int j = 0; j < 3; j++)
                        await PlayerCmd.GainEnergy(1m, Owner.Player!);
                }
            }
        }
    }

    private sealed class SyntheticCrossHookGuardPower : PowerModel
    {
        private bool _guard;
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Single;
        private bool GuardFlag
        {
            get => _guard;
            set => _guard = value;
        }
        public override Task AfterApplied(Creature? applier, CardModel? cardSource)
        {
            GuardFlag = true;
            return Task.CompletedTask;
        }
        public override async Task AfterDamageReceived(PlayerChoiceContext choiceContext, Creature target, DamageResult result, ValueProp props, Creature? dealer, CardModel? cardSource)
        {
            if (target == Owner && !GuardFlag)
            {
                GuardFlag = true;
                await PlayerCmd.GainEnergy(1m, Owner.Player!);
                GuardFlag = false;
            }
        }
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

    private sealed class SyntheticRefAliasPower : PowerModel
    {
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Single;
        public override Task AfterEnergySpent(CardModel card, int amount)
        {
            ref int alias = ref amount;
            alias = 0;
            return PlayerCmd.GainEnergy(amount, Owner.Player!);
        }
    }

    private sealed class SyntheticNestedForeachPower : PowerModel
    {
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Single;
        public override async Task AfterSideTurnStart(CombatSide side, IReadOnlyList<Creature> participants, ICombatState combatState)
        {
            if (!participants.Contains(Owner))
                return;
            foreach (var _ in PileType.Hand.GetPile(Owner.Player!).Cards)
                foreach (var __ in PileType.Discard.GetPile(Owner.Player!).Cards)
                    await PlayerCmd.GainEnergy(1m, Owner.Player!);
        }
    }

    private sealed class SyntheticCardFactorySelectPower : PowerModel
    {
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Single;
        public override async Task AfterSideTurnStart(CombatSide side, IReadOnlyList<Creature> participants, ICombatState combatState)
        {
            if (!participants.Contains(Owner))
                return;
            var pool = Owner.Player!.Character.CardPool.GetUnlockedCards(Owner.Player.UnlockState, Owner.Player.RunState.CardMultiplayerConstraint)
                .Where(c => c.Type == CardType.Attack);
            var replaced = CardFactory.GetDistinctForCombat(Owner.Player, pool, 3, Owner.Player.RunState.Rng.CombatCardGeneration)
                .Select(_ => combatState.CreateCard<DefendIronclad>(Owner.Player))
                .First();
            await CardPileCmd.AddGeneratedCardToCombat(replaced, PileType.Hand, Owner.Player);
        }
    }

    private sealed class SyntheticShadowBindEvent : EventModel
    {
        protected override IReadOnlyList<EventOption> GenerateInitialOptions() => [];
        public Task Pay(int amount) => Give(9, amount);
        private Task Give(int amount, int actual) => PlayerCmd.GainGold(actual, Owner!);
    }

    private sealed class SyntheticTypedEchoFormPower : PowerModel
    {
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Counter;
        public override int ModifyCardPlayCount(CardModel card, Creature? target, int playCount)
        {
            if (card.Owner.Creature != Owner || card.Type != CardType.Attack)
                return playCount;
            if (CombatManager.Instance.History.CardPlaysStarted.Count(e => e.Actor == Owner && e.CardPlay.IsFirstInSeries && e.HappenedThisTurn(CombatState)) >= Amount)
                return playCount;
            return playCount + 1;
        }
    }

    private sealed class SyntheticStandaloneAttackCard : CardModel
    {
        public SyntheticStandaloneAttackCard() : base(1, CardType.Attack, CardRarity.Token, TargetType.AllEnemies) { }
        protected override async Task OnPlay(PlayerChoiceContext choiceContext, CardPlay cardPlay)
        {
            var attack = DamageCmd.Attack(25m).TargetingAllOpponents(CombatState!);
            attack.WithHitCount(3);
            await attack.Execute(choiceContext);
        }
    }

    private sealed class SyntheticStandaloneAttackMutatorPower : PowerModel
    {
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Single;
        public override async Task AfterSideTurnStart(CombatSide side, IReadOnlyList<Creature> participants, ICombatState combatState)
        {
            if (!participants.Contains(Owner))
                return;
            var card = combatState.CreateCard<SyntheticStandaloneAttackCard>(Owner.Player!);
            await CardPileCmd.AddGeneratedCardToCombat(card, PileType.Hand, Owner.Player!);
        }
    }

    private sealed class SyntheticMutatedAutomationPower : PowerModel
    {
        private sealed class Data { public int cardsLeft = 10; }
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Counter;
        protected override IEnumerable<DynamicVar> CanonicalVars => [new DynamicVar("BaseCards", 10m)];
        protected override object InitInternalData() => new Data();
        public override async Task AfterCardDrawn(PlayerChoiceContext choiceContext, CardModel card, bool fromHandDraw)
        {
            if (card.Owner == Owner.Player)
            {
                var data = GetInternalData<Data>();
                data.cardsLeft--;
                if (data.cardsLeft <= 8)
                {
                    await PlayerCmd.GainEnergy(Amount, Owner.Player);
                    data.cardsLeft = 10;
                }
            }
        }
    }

    private sealed class SyntheticMutatedOrbitPower : PowerModel
    {
        private sealed class Data { public int energySpent; public int triggerCount; }
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Counter;
        protected override IEnumerable<DynamicVar> CanonicalVars => [new EnergyVar(4)];
        protected override object InitInternalData() => new Data();
        public override async Task AfterEnergySpent(CardModel card, int amount)
        {
            if (card.Owner.Creature == Owner && amount > 0)
            {
                var data = GetInternalData<Data>();
                data.energySpent += amount * 2;
                int triggers = data.energySpent / 4 - data.triggerCount;
                if (triggers > 0)
                {
                    await PlayerCmd.GainEnergy(Amount * triggers, Owner.Player!);
                    data.triggerCount += triggers;
                }
            }
        }
    }

    private sealed class SyntheticMutatedVoidFormPower : PowerModel
    {
        private sealed class Data { public int cardsPlayedThisTurn; }
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Counter;
        protected override object InitInternalData() => new Data();
        public override bool TryModifyEnergyCostInCombatLate(CardModel card, decimal originalCost, out decimal modifiedCost)
        {
            modifiedCost = originalCost;
            if (card.Owner.Creature != Owner || GetInternalData<Data>().cardsPlayedThisTurn >= Amount) return false;
            modifiedCost = 0m;
            return true;
        }
        public override bool TryModifyStarCost(CardModel card, decimal originalCost, out decimal modifiedCost)
        {
            modifiedCost = originalCost;
            if (card.Owner.Creature != Owner || GetInternalData<Data>().cardsPlayedThisTurn >= Amount) return false;
            modifiedCost = 0m;
            return true;
        }
        public override Task AfterCardPlayed(PlayerChoiceContext choiceContext, CardPlay cardPlay)
        {
            if (cardPlay.Card.Owner.Creature == Owner && cardPlay.IsAutoPlay && cardPlay.IsLastInSeries)
                GetInternalData<Data>().cardsPlayedThisTurn++;
            return Task.CompletedTask;
        }
        public override Task BeforeSideTurnStart(PlayerChoiceContext choiceContext, CombatSide side, IReadOnlyList<Creature> participants, ICombatState combatState)
        {
            if (participants.Contains(Owner))
                GetInternalData<Data>().cardsPlayedThisTurn = 0;
            return Task.CompletedTask;
        }
    }

    private sealed class SyntheticMutatedImitationPower : PowerModel
    {
        private sealed class Data { public readonly List<(CardModel Card, CardModel Clone)> list = []; }
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Counter;
        protected override object InitInternalData() => new Data();
        public override Task BeforeCardPlayed(CardPlay cardPlay)
        {
            if (cardPlay.Card.Type != CardType.Power || !cardPlay.IsLastInSeries)
                return Task.CompletedTask;
            GetInternalData<Data>().list.Add((cardPlay.Card, cardPlay.Card.CreateCloneForPlayer(Owner.Player!)));
            return Task.CompletedTask;
        }
        public override async Task AfterCardPlayed(PlayerChoiceContext choiceContext, CardPlay cardPlay)
        {
            var data = GetInternalData<Data>();
            int idx = data.list.FindIndex(p => p.Card == cardPlay.Card);
            if (idx >= 0)
            {
                var clone = data.list[idx].Clone;
                data.list.RemoveAt(idx);
                await PowerCmd.Decrement(this);
                await CardCmd.AutoPlay(choiceContext, clone, null);
            }
        }
    }

    private sealed class SyntheticMutatedHellraiserPower : PowerModel
    {
        private sealed class Data { public int count; }
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Single;
        protected override object InitInternalData() => new Data();
        public override async Task AfterCardDrawnEarly(PlayerChoiceContext choiceContext, CardModel card, bool fromHandDraw)
        {
            if (card.Owner.Creature != Owner || !card.Tags.Contains(CardTag.Strike))
                return;
            var data = GetInternalData<Data>();
            bool ok = true;
            if (Owner.CombatState!.HittableEnemies.Any(c => c.HpDisplay.IsInfinite()))
            {
                if (data.count >= 9) ok = false;
                data.count++;
            }
            if (ok)
                await CardCmd.AutoPlay(choiceContext, card, null);
        }
        public override Task AfterSideTurnEnd(PlayerChoiceContext choiceContext, CombatSide side, IEnumerable<Creature> participants)
        {
            if (participants.Contains(Owner))
                GetInternalData<Data>().count = 0;
            return Task.CompletedTask;
        }
    }

    private sealed class SyntheticMutatedJugglingPower : PowerModel
    {
        private sealed class Data { public int count; public CardModel? card; }
        public override PowerType Type => PowerType.Buff;
        public override PowerStackType StackType => PowerStackType.Counter;
        protected override object InitInternalData() => new Data();
        public override Task AfterApplied(Creature? applier, CardModel? cardSource)
        {
            GetInternalData<Data>().count = CombatManager.Instance.History.CardPlaysStarted.Count(e => e.CardPlay.Card.Type == CardType.Attack && e.CardPlay.Player == Owner.Player && e.HappenedThisTurn(CombatState));
            return Task.CompletedTask;
        }
        public override Task BeforeCardPlayed(CardPlay cardPlay)
        {
            if (cardPlay.Card.Owner != Owner.Player || cardPlay.Card.Type != CardType.Attack) return Task.CompletedTask;
            GetInternalData<Data>().count++;
            if (GetInternalData<Data>().count == 3) GetInternalData<Data>().card = cardPlay.Card;
            return Task.CompletedTask;
        }
        public override async Task AfterCardPlayed(PlayerChoiceContext choiceContext, CardPlay cardPlay)
        {
            var data = GetInternalData<Data>();
            if (data.card == cardPlay.Card)
            {
                data.card = null;
                for (int i = 0; i < 1; i++)
                    await CardPileCmd.AddGeneratedCardToCombat(cardPlay.Card.CreateClone(), PileType.Hand, Owner.Player!);
            }
        }
        public override Task AfterSideTurnEnd(PlayerChoiceContext choiceContext, CombatSide side, IEnumerable<Creature> participants)
        {
            if (participants.Contains(Owner)) GetInternalData<Data>().count = 0;
            return Task.CompletedTask;
        }
    }

    private sealed class SyntheticMutatedPoisonPower : PowerModel
    {
        public override PowerType Type => PowerType.Debuff;
        public override PowerStackType StackType => PowerStackType.Counter;
        private int TriggerCount => Math.Min(Amount, 1 + Owner.CombatState!.GetOpponentsOf(Owner).Where(c => c.IsAlive).Sum(a => a.GetPowerAmount<AccelerantPower>()));
        public override async Task AfterSideTurnStart(CombatSide side, IReadOnlyList<Creature> participants, ICombatState combatState)
        {
            if (participants.Contains(Owner))
                await Trigger();
        }
        public async Task Trigger()
        {
            _ = TriggerCount;
            for (int i = 0; i < 1; i++)
            {
                await CreatureCmd.Damage(new ThrowingPlayerChoiceContext(), Owner, Amount, ValueProp.Unblockable | ValueProp.Unpowered, null, null);
                if (Owner.IsAlive)
                    await PowerCmd.Decrement(this);
            }
        }
    }
}
