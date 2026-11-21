using Godot;
using HarmonyLib;
using MegaCrit.Sts2.Core.Nodes.Cards;

namespace Sts2Mod.Ui;

[HarmonyPatch(typeof(NCard), "UpdateEnchantmentVisuals")]
internal static class EnchantmentBadgePresentation
{
    private static void Postfix(NCard __instance)
    {
        // An effect card already renders its badge through the enchantment viewport.
        if (__instance.EnchantmentVfxOverride is { Visible: true, Texture: ViewportTexture })
            __instance.EnchantmentTab.Hide();
    }
}
