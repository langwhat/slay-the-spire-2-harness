using System.Collections.Immutable;
using System.Reflection;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Models;
using Sts2Mod.Core.Observation;

namespace Sts2Mod.Observation;

/// <summary>
/// Renders native card facts, target-specific descriptions, and upgrade previews on the game thread
/// while isolating temporary dynamic-variable preview mutations from the live card instance.
/// </summary>
internal static class CardPreviewReader
{
    private static readonly FieldInfo CardVarsField =
        typeof(CardModel).GetField("_dynamicVars", BindingFlags.Instance | BindingFlags.NonPublic)
        ?? throw new MissingFieldException(nameof(CardModel), "_dynamicVars");

    private static readonly FieldInfo EnchantmentVarsField =
        typeof(EnchantmentModel).GetField("_dynamicVars", BindingFlags.Instance | BindingFlags.NonPublic)
        ?? throw new MissingFieldException(nameof(EnchantmentModel), "_dynamicVars");

    internal sealed record RenderedCard(
        CardEntry Entry,
        string? Text,
        CardModifierDescriptions ModifierDescriptions);

    public static RenderedCard ReadLiveCard(
        CardModel card,
        PileType pile,
        bool? playable = null,
        ImmutableArray<TargetTextPreview>? previews = null,
        bool? needsTarget = null) =>
        ReadCore(
            card,
            pile,
            unpowered: false,
            previewMode: CardPreviewMode.Normal,
            target: null,
            definition: false,
            playable: playable,
            previews: previews,
            needsTarget: needsTarget);

    public static string ReadTargetText(CardModel card, PileType pile, Creature target) =>
        ReadCore(
            card,
            pile,
            unpowered: false,
            previewMode: CardPreviewMode.Normal,
            target: target,
            definition: false).Text ?? "";

    public static CardDefinitionPreview ReadDefinitionPreview(CardModel card, bool definition = false)
    {
        var rendered = ReadCore(
            card,
            PileType.None,
            unpowered: definition,
            previewMode: CardPreviewMode.Normal,
            target: null,
            definition: definition);
        return new CardDefinitionPreview(rendered.Entry, rendered.Text);
    }

    public static CardDefinitionPreview ReadUpgradedPreview(CardModel card, bool definition = false)
    {
        var upgraded = (CardModel)card.MutableClone();
        upgraded.UpgradeInternal();
        upgraded.UpgradePreviewType = card.Pile?.IsCombatPile == true || card.CombatState is not null
            ? CardUpgradePreviewType.Combat
            : CardUpgradePreviewType.Deck;

        var rendered = ReadCore(
            upgraded,
            PileType.None,
            unpowered: definition,
            previewMode: CardPreviewMode.Upgrade,
            target: null,
            definition: definition);
        return new CardDefinitionPreview(rendered.Entry, rendered.Text);
    }

    private static RenderedCard ReadCore(
        CardModel card,
        PileType pile,
        bool unpowered,
        CardPreviewMode previewMode,
        Creature? target,
        bool definition,
        bool? playable = null,
        ImmutableArray<TargetTextPreview>? previews = null,
        bool? needsTarget = null)
    {
        var originalVars = card.DynamicVars;
        var enchantment = card.Enchantment;
        var originalEnchantmentVars = enchantment?.DynamicVars;
        var preview = originalVars.Clone(card);
        var enchantmentPreview = originalEnchantmentVars?.Clone(enchantment!);
        preview.ClearPreview();
        enchantmentPreview?.ClearPreview();

        try
        {
            CardVarsField.SetValue(card, preview);
            if (enchantment is not null)
                EnchantmentVarsField.SetValue(enchantment, enchantmentPreview);

            if (!unpowered)
            {
                card.UpdateDynamicVarPreview(previewMode, target, preview);
                if (enchantmentPreview is not null)
                    card.UpdateDynamicVarPreview(previewMode, target, enchantmentPreview);
            }

            var text = previewMode == CardPreviewMode.Upgrade
                ? card.GetDescriptionForUpgradePreview()
                : card.GetDescriptionForPile(pile, target);

            var displayEnergy = definition
                ? card.EnergyCost.GetWithModifiers(CostModifiers.Local)
                : card.EnergyCost.GetWithModifiers(CostModifiers.All);
            var localEnergy = card.EnergyCost.GetWithModifiers(CostModifiers.Local);
            var localStar = card.CurrentStarCost;
            var starCost = definition ? localStar : card.GetStarCostWithModifiers();
            var costsX = card.EnergyCost.CostsX;
            var starCostsX = card.HasStarCostX;

            var keywords = card.Keywords
                .Where(keyword => keyword != CardKeyword.None)
                .Select(keyword => keyword.ToString().ToLowerInvariant())
                .Order(StringComparer.Ordinal)
                .ToImmutableArray();

            var localKeywords = card.GetKeywordsWithSources(KeywordSources.Local)
                .Where(keyword => keyword != CardKeyword.None)
                .Select(keyword => keyword.ToString().ToLowerInvariant())
                .Order(StringComparer.Ordinal)
                .ToImmutableArray();

            var enchantmentModifier = enchantment is null
                ? null
                : new CardModifier(
                    enchantment.Id.ToString(),
                    enchantment.Title.GetFormattedText(),
                    enchantment.Amount)
                {
                    Status = enchantment.Status.ToString().ToLowerInvariant(),
                };

            var affliction = card.Affliction;
            var afflictionModifier = affliction is null
                ? null
                : new CardModifier(
                    affliction.Id.ToString(),
                    affliction.Title.GetFormattedText(),
                    affliction.Amount);

            var cost = new ResourceCosts(
                ResourceCost.FromNative(displayEnergy, costsX),
                ResourceCost.FromNative(starCost, starCostsX));

            var localCost = new ResourceCosts(
                ResourceCost.FromNative(localEnergy, costsX),
                ResourceCost.FromNative(localStar, starCostsX));

            var costWithoutGlobalEffects = localCost != cost
                ? localCost
                : null;

            var entry = new CardEntry(
                card.Id.ToString(),
                card.Title,
                card.CurrentUpgradeLevel,
                card.Type.ToString().ToLowerInvariant(),
                card.Pool.Id.ToString(),
                card.Rarity.ToString().ToLowerInvariant(),
                card.IsUpgradable,
                card.IsRemovable,
                keywords,
                card.ShouldRetainThisTurn,
                card.ExhaustOnNextPlay,
                card.HasTurnEndInHandEffect,
                enchantmentModifier,
                afflictionModifier,
                cost)
            {
                LocalKeywords = localKeywords,
                CostWithoutGlobalEffects = costWithoutGlobalEffects,
                IsSlyThisTurn = card.IsSlyThisTurn ? true : null,
                Playable = playable,
                NeedsTarget = needsTarget,
                Previews = previews,
            };

            return new RenderedCard(
                entry,
                text,
                new CardModifierDescriptions(
                    enchantment?.DynamicDescription.GetFormattedText(),
                    affliction?.DynamicDescription.GetFormattedText()));
        }
        finally
        {
            CardVarsField.SetValue(card, originalVars);
            if (enchantment is not null)
                EnchantmentVarsField.SetValue(enchantment, originalEnchantmentVars);
        }
    }
}
