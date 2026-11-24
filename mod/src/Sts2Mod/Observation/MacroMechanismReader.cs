using System.Globalization;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Models;

namespace Sts2Mod.Observation;

internal static class MacroMechanismReader
{
    public const string ShopRemovalId = "MACRO.SHOP_CARD_REMOVAL";
    public const string ActDoubleBossId = "MACRO.ACT_DOUBLE_BOSS";
    public const string XCostRuleId = "MACRO.X_COST";
    public const string OstyRoutingId = "MACRO.OSTY_DAMAGE_ROUTING";
    public const string OrbQueueRuleId = "MACRO.ORB_QUEUE";

    public static string ReadShopRemoval(Player player, MerchantCardRemovalEntry removalEntry)
    {
        var increase = MerchantCardRemovalEntry.PriceIncrease;
        var used = player.ExtraFields.CardShopRemovalsUsed;
        var current = removalEntry.Cost;
        var baseCost = Math.Max(0, current - increase * used);
        var next = current + increase;
        return string.Create(
            CultureInfo.InvariantCulture,
            $"Shop card removal: once per shop; cost increases by +{increase} Gold after each successful shop removal (base: {baseCost}, prior removals: {used}, current: {current} Gold, next shop: {next} Gold). Canceling card selection does not spend Gold or consume the removal.");
    }

    public static string? ReadActBossContinuity(IReadOnlyList<EncounterModel> upcomingBosses)
    {
        if (upcomingBosses.Count < 2)
            return null;
        return $"Act double-boss continuity: defeating the first Act boss ({upcomingBosses[0].Title.GetFormattedText()} / {upcomingBosses[0].Id}) immediately transitions into the second Act boss ({upcomingBosses[1].Title.GetFormattedText()} / {upcomingBosses[1].Id}) with no heal or rest between fights (HP, deck, relics, and potions carry over).";
    }

    public static string XCostRule()
        => "X-cost cards spend all current Energy when played (X = Energy spent; playing at 0 Energy sets X = 0); replays and autoplays reuse the original X.";

    public static string OstyDamageRoutingRule()
        => $"Incoming attack damage targets {NativeRuleText.ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty))} before the player (excess damage overflows to the player); post-Osty HP-loss effects apply after redirection.";

    public static string OrbQueueRule()
        => "Orb queue: channeling into a full queue automatically evokes the front (oldest) orb first; Focus modifies only Focus-scaled orb values (not Plasma).";
}
