using System.Globalization;
using System.Reflection;
using HarmonyLib;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Hooks;
using MegaCrit.Sts2.Core.Models;

namespace Sts2Mod.Observation;

internal static class MacroMechanismReader
{
    public const string ShopRemovalId = "MACRO.SHOP_CARD_REMOVAL";
    public const string ActDoubleBossId = "MACRO.ACT_DOUBLE_BOSS";
    public const string XCostRuleId = "MACRO.X_COST";
    public const string OstyRoutingId = "MACRO.OSTY_DAMAGE_ROUTING";
    public const string OstySummonId = "MACRO.OSTY_SUMMON";
    public const string OrbQueueRuleId = "MACRO.ORB_QUEUE";
    public const string RegentStarsRuleId = "MACRO.REGENT_STARS";
    public const string ForgeSovereignBladeRuleId = "MACRO.FORGE_SOVEREIGN_BLADE";
    public const string SlyRuleId = "MACRO.SLY";
    public const string FoolArrowsRuleId = "MACRO.FOOL_ARROWS";
    public const string FoolMelodiesRuleId = "MACRO.FOOL_MELODIES";

    private static readonly FieldInfo? MerchantEntryRawCost = AccessTools.Field(typeof(MerchantEntry), "_cost");
    private static readonly PropertyInfo? RemovalBaseCost = AccessTools.Property(typeof(MerchantCardRemovalEntry), "BaseCost");

    public static string ReadShopRemoval(Player player, MerchantCardRemovalEntry removalEntry)
    {
        var increase = MerchantCardRemovalEntry.PriceIncrease;
        var used = player.ExtraFields.CardShopRemovalsUsed;
        var listBase = RemovalBaseCost?.GetValue(null) is int nativeBase
            ? nativeBase
            : (MerchantEntryRawCost?.GetValue(removalEntry) is int raw && !removalEntry.Used
                ? Math.Max(0, raw - increase * used)
                : (int?)null);

        if (removalEntry.Used)
        {
            if (listBase is { } usedBase)
            {
                var nextShopList = usedBase + increase * used;
                var nextShopPayable = TryComputeModifiedPrice(player, removalEntry, nextShopList);
                var nextClause = nextShopPayable is { } modified && modified != nextShopList
                    ? $"next shop at current price modifiers: {modified} Gold (list price: {nextShopList} Gold)"
                    : $"next shop list price: {nextShopList} Gold";
                return string.Create(
                    CultureInfo.InvariantCulture,
                    $"Shop card removal: already used in this shop (once per shop; pre-discount list base: {usedBase}, +{increase} Gold per removal, removals used so far: {used}, {nextClause}).");
            }
            return string.Create(
                CultureInfo.InvariantCulture,
                $"Shop card removal: already used in this shop (once per shop; pre-discount list price increases by +{increase} Gold per removal, removals used so far: {used}).");
        }

        var current = removalEntry.Cost;
        var rawCurrent = listBase is { } knownBase
            ? knownBase + increase * used
            : (MerchantEntryRawCost?.GetValue(removalEntry) is int rawCost ? rawCost : (int?)null);

        if (rawCurrent is { } listCurrent)
        {
            var effectiveBase = listBase ?? Math.Max(0, listCurrent - increase * used);
            var listNext = listCurrent + increase;
            if (listCurrent == current)
            {
                return string.Create(
                    CultureInfo.InvariantCulture,
                    $"Shop card removal: once per shop; list price increases by +{increase} Gold after each successful shop removal (list base: {effectiveBase}, prior removals: {used}, current: {current} Gold, next shop list price: {listNext} Gold). Canceling card selection does not spend Gold or consume the removal.");
            }

            var discountedNext = TryComputeModifiedPrice(player, removalEntry, listNext);
            var nextClause = discountedNext is { } nextPayable
                ? $", next shop at current price modifiers: {nextPayable} Gold (list price: {listNext} Gold)"
                : $", next shop list price: {listNext} Gold";
            return string.Create(
                CultureInfo.InvariantCulture,
                $"Shop card removal: once per shop; pre-discount list price increases by +{increase} Gold after each successful shop removal (list base: {effectiveBase}, prior removals: {used}, current list price: {listCurrent} Gold; current payable after price modifiers: {current} Gold{nextClause}). Canceling card selection does not spend Gold or consume the removal.");
        }

        return string.Create(
            CultureInfo.InvariantCulture,
            $"Shop card removal: once per shop; pre-discount list price increases by +{increase} Gold after each successful shop removal (prior removals: {used}, current payable: {current} Gold). Canceling card selection does not spend Gold or consume the removal.");
    }

    private static int? TryComputeModifiedPrice(Player player, MerchantCardRemovalEntry removalEntry, int listPrice)
    {
        try
        {
            if (player.RunState is { } runState)
                return (int)Hook.ModifyMerchantPrice(runState, player, removalEntry, listPrice);
        }
        catch
        {
            return null;
        }
        return null;
    }

    public static string? ReadActBossContinuity(IReadOnlyList<EncounterModel> upcomingBosses)
    {
        if (upcomingBosses.Count < 2)
            return null;
        return $"Act double-boss continuity: defeating the first Act boss ({upcomingBosses[0].Title.GetFormattedText()} / {upcomingBosses[0].Id}) transitions into the second Act boss ({upcomingBosses[1].Title.GetFormattedText()} / {upcomingBosses[1].Id}) after any post-combat extra rewards, with no inter-act rest or full heal between fights (normal combat victory effects still trigger; HP, deck, relics, and potions carry over).";
    }

    public static string XCostRule()
        => "X-cost cards spend all current Energy (for Energy X-cost) or all current Stars (for Star X-cost) on manual play (base X = amount spent, 0 at 0); replays reuse the original captured X; auto-plays spend 0 Energy/Stars and capture current Energy/Stars as base X unless the triggering effect preserves X; X-value modifiers adjust the resolved X for both Energy and Star X-cost cards.";

    public static string OstyDamageRoutingRule()
        => $"While {NativeRuleText.ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty))} is alive, unblocked powered attack damage targeting the player (after the player's Block is applied) is redirected to {NativeRuleText.ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty))} first (excess damage overflows to the player); post-Osty HP-loss effects apply after redirection.";

    public static string OstySummonRule()
    {
        var osty = NativeRuleText.ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty));
        var dieForYou = NativeRuleText.ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Powers.DieForYouPower));
        return $"Summon {osty}: if {osty} is missing or dead, Summon N creates or revives {osty} with N HP and N Max HP and applies {dieForYou} (reviving a dead {osty} also triggers {osty} revival effects); if {osty} is already alive, Summon N increases its Max HP and heals it by N. While {osty} is missing or dead, most {osty}-attack and {osty}-gated cards still report playable=true and non-zero native damage previews and still spend Energy if played, but deal no {osty} attack damage.";
    }

    public static string OrbQueueRule()
    {
        var focus = NativeRuleText.ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Powers.FocusPower));
        var plasma = NativeRuleText.ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Orbs.PlasmaOrb));
        return $"Orb queue: slots are ordered front-to-back (slots[0] is front / oldest / next-to-evoke; slots[-1] is back / newest / last). Channeling into a full queue automatically evokes slots[0] first; {focus} modifies only {focus}-scaled orb values (not {plasma}).";
    }

    public static string RegentStarsRule()
        => "Stars: combat resource starting at 0 each combat (unless granted by relics or effects) that persists across turns without resetting at turn end; spent on Star-cost cards and Star-consuming effects.";

    public static string ForgeSovereignBladeRule()
    {
        var blade = NativeRuleText.ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Cards.SovereignBlade));
        return $"Forge: if the player has no non-dupe {blade} outside the exhaust pile, creates 1 {blade} (base 10 damage, Retain) in hand (overflows to discard if hand is full); then increases the base damage of every non-dupe {blade} across all combat piles (including the newly created copy and exhausted copies) by the Forge amount and triggers on-Forge effects.";
    }

    public static string SlyRule()
        => "Sly: is_sly_this_turn=true marks any card with Sly active this turn (either permanently via the Sly keyword or temporarily via single-turn Sly, which does not add sly to keywords and expires at end of turn). When such a card is discarded from hand by a card/effect during your turn, it moves to the discard pile (and any accompanying draw from that discard-and-draw effect finishes) and then auto-plays for free (random legal target if targeted; blocked by Unplayable or play-prevention effects). End-of-turn hand flush does not trigger Sly.";

    public static string FoolArrowsRule()
    {
        var impaled = NativeRuleText.ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Powers.ImpaledPower));
        return $"Fool arrows: arrow-shooting cards consume 1 retrieved arrow slot (turning it away) on each play or replay. When 0 retrieved arrows remain, shooting cards still report playable=true and non-zero native damage previews and still spend Energy if played, but fire no arrow, deal no arrow damage, do not apply {impaled}, and skip arrow-gated follow-up effects. Multi-hit shooting cards use 1 arrow for all hits; replaying a shooting card requires another retrieved arrow unless an arrow returned first. On a shot, arrow damage resolves before {impaled} is applied, so that shot does not gain impaled-target bonuses from its own impale. If the shot kills the target or an impaled enemy dies, its impaled arrows dislodge back to retrieved (triggering dislodge hooks).";
    }

    public static string FoolMelodiesRule()
        => "Fool melodies: Wah, Cho, and Ki tags are recorded in Combat state melodies when a card play with that tag starts this turn (counting replays and auto-plays) and reset at turn end. Effects counting melodies this turn count distinct activated tags (0-3).";
}
