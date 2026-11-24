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

    public static string ReadShopRemoval(Player player, MerchantCardRemovalEntry removalEntry)
    {
        var increase = MerchantCardRemovalEntry.PriceIncrease;
        var used = player.ExtraFields.CardShopRemovalsUsed;
        var current = removalEntry.Cost;
        var rawCurrent = MerchantEntryRawCost?.GetValue(removalEntry) is int raw ? raw : (int?)null;

        if (rawCurrent is { } listCurrent)
        {
            var listBase = Math.Max(0, listCurrent - increase * used);
            var listNext = listCurrent + increase;
            if (listCurrent == current)
            {
                return string.Create(
                    CultureInfo.InvariantCulture,
                    $"Shop card removal: once per shop; list price increases by +{increase} Gold after each successful shop removal (list base: {listBase}, prior removals: {used}, current: {current} Gold, next shop list price: {listNext} Gold). Canceling card selection does not spend Gold or consume the removal.");
            }

            int? discountedNext = null;
            try
            {
                if (player.RunState is { } runState)
                    discountedNext = (int)Hook.ModifyMerchantPrice(runState, player, removalEntry, listNext);
            }
            catch
            {
                discountedNext = null;
            }

            var nextClause = discountedNext is { } nextPayable
                ? $", next shop at current price modifiers: {nextPayable} Gold (list price: {listNext} Gold)"
                : $", next shop list price: {listNext} Gold";
            return string.Create(
                CultureInfo.InvariantCulture,
                $"Shop card removal: once per shop; pre-discount list price increases by +{increase} Gold after each successful shop removal (list base: {listBase}, prior removals: {used}, current list price: {listCurrent} Gold; current payable after price modifiers: {current} Gold{nextClause}). Canceling card selection does not spend Gold or consume the removal.");
        }

        return string.Create(
            CultureInfo.InvariantCulture,
            $"Shop card removal: once per shop; pre-discount list price increases by +{increase} Gold after each successful shop removal (prior removals: {used}, current payable: {current} Gold). Canceling card selection does not spend Gold or consume the removal.");
    }

    public static string? ReadActBossContinuity(IReadOnlyList<EncounterModel> upcomingBosses)
    {
        if (upcomingBosses.Count < 2)
            return null;
        return $"Act double-boss continuity: defeating the first Act boss ({upcomingBosses[0].Title.GetFormattedText()} / {upcomingBosses[0].Id}) immediately transitions into the second Act boss ({upcomingBosses[1].Title.GetFormattedText()} / {upcomingBosses[1].Id}) with no heal or rest between fights (HP, deck, relics, and potions carry over).";
    }

    public static string XCostRule()
        => "X-cost cards spend all current Energy on manual play (base X = Energy spent, 0 at 0 Energy); replays reuse the original captured X; auto-plays spend 0 Energy and capture current Energy as base X unless the triggering effect preserves X; X-value modifiers still adjust the resolved X effect.";

    public static string OstyDamageRoutingRule()
        => $"Incoming attack damage targets {NativeRuleText.ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty))} before the player (excess damage overflows to the player); post-Osty HP-loss effects apply after redirection.";

    public static string OrbQueueRule()
        => "Orb queue: channeling into a full queue automatically evokes the front (oldest) orb first; Focus modifies only Focus-scaled orb values (not Plasma).";
}
