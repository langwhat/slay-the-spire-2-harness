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
    public const string OrbQueueRuleId = "MACRO.ORB_QUEUE";

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
        => "X-cost cards spend all current Energy on manual play (base X = Energy spent, 0 at 0 Energy); replays reuse the original captured X; auto-plays spend 0 Energy and capture current Energy as base X unless the triggering effect preserves X; X-value modifiers still adjust the resolved X effect.";

    public static string OstyDamageRoutingRule()
        => $"While {NativeRuleText.ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty))} is alive, unblocked powered attack damage targeting the player (after the player's Block is applied) is redirected to {NativeRuleText.ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty))} first (excess damage overflows to the player); post-Osty HP-loss effects apply after redirection.";

    public static string OrbQueueRule()
        => "Orb queue: channeling into a full queue automatically evokes the front (oldest) orb first; Focus modifies only Focus-scaled orb values (not Plasma).";
}
