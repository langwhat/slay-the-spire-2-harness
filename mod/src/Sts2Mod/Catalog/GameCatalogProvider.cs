using System.Collections.Concurrent;
using System.Collections.Immutable;
using Godot;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Models;
using Sts2Mod.Core.Catalog;
using Sts2Mod.Core.Developer;
using Sts2Mod.Core.Observation;
using Sts2Mod.Observation;

namespace Sts2Mod.Catalog;

/// <summary>
/// Serves <c>/catalog</c> queries and laboratory definition checks on the native game thread.
/// </summary>
internal sealed class GameCatalogProvider
{
    private sealed record CatalogWork(string Body, TaskCompletionSource<(int StatusCode, string Json)> Completion);
    private sealed record ScenarioWork(CombatEvalScenario Recipe, TaskCompletionSource<bool> Completion);

    private readonly ConcurrentQueue<CatalogWork> _requests = new();
    private readonly ConcurrentQueue<ScenarioWork> _scenarios = new();

    public Task<(int StatusCode, string Json)> ReadAsync(string body)
    {
        var completion = new TaskCompletionSource<(int StatusCode, string Json)>(
            TaskCreationOptions.RunContinuationsAsynchronously);
        _requests.Enqueue(new CatalogWork(body, completion));
        return completion.Task;
    }

    public Task<bool> ValidateScenarioAsync(CombatEvalScenario recipe)
    {
        var completion = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        _scenarios.Enqueue(new ScenarioWork(recipe, completion));
        return completion.Task;
    }

    public void Advance()
    {
        while (_scenarios.TryDequeue(out var scenario))
        {
            try
            {
                var cards = ModelDb.AllCards
                    .Where(model => !model.IsMock)
                    .Select(model => model.Id.ToString())
                    .ToHashSet(StringComparer.Ordinal);
                var relics = ModelDb.AllRelics
                    .Where(model => !model.IsMock)
                    .Select(model => model.Id.ToString())
                    .ToHashSet(StringComparer.Ordinal);
                var potions = ModelDb.AllPotions
                    .Where(model => !model.IsMock)
                    .Select(model => model.Id.ToString())
                    .ToHashSet(StringComparer.Ordinal);
                var encounters = ModelDb.All
                    .OfType<EncounterModel>()
                    .Where(model => !model.IsMock)
                    .Select(model => model.Id.ToString())
                    .ToHashSet(StringComparer.Ordinal);

                var recipe = scenario.Recipe;
                scenario.Completion.SetResult(
                    recipe.Deck.All(cards.Contains)
                    && recipe.Relics.All(relics.Contains)
                    && recipe.Potions.All(potions.Contains)
                    && encounters.Contains(recipe.Encounter));
            }
            catch (Exception error)
            {
                scenario.Completion.SetException(error);
            }
        }

        if (!_requests.TryDequeue(out var request))
            return;

        try
        {
            var language = LocManager.Instance?.Language ?? "eng";
            request.Completion.SetResult(CatalogReader.Read(request.Body, Capture, language));
        }
        catch (Exception error)
        {
            GD.PrintErr($"[sts2agent] Native catalog read failed: {error}");
            request.Completion.SetResult(CatalogReader.Unavailable());
        }
    }

    private static CatalogPublication Capture()
    {
        var cards = ModelDb.AllCards.Where(card => !card.IsMock).ToArray();
        if (cards.Length == 0)
            throw new InvalidOperationException("The native card database is not initialized.");

        var records = ImmutableArray.CreateBuilder<CatalogRecord>();
        foreach (var canonical in cards)
        {
            var card = canonical.ToMutable();
            var preview = CardPreviewReader.ReadDefinitionPreview(card, definition: true);
            records.Add(new CatalogRecord(
                CatalogCategory.Card,
                preview.DefinitionId,
                preview.Name,
                preview.Text,
                () => new CardDefinition(
                    preview.DefinitionId,
                    preview.Name,
                    preview,
                    new UpgradePreviewReader(error =>
                            GD.PrintErr($"[sts2agent] Catalog upgrade preview unavailable for {canonical.Id}: {error}"))
                        .Read(card.IsUpgradable, () => CardPreviewReader.ReadUpgradedPreview(card, definition: true))))
            {
                Card = preview,
            });
        }

        foreach (var canonical in ModelDb.AllRelics.Where(model => !model.IsMock))
        {
            var relic = canonical.ToMutable();
            var native = new NativeRelic(
                relic.Id.ToString(),
                relic.Title.GetFormattedText(),
                relic.DynamicDescription.GetFormattedText(),
                relic.Rarity.ToString().ToLowerInvariant());
            records.Add(new CatalogRecord(
                CatalogCategory.Relic,
                native.DefinitionId,
                native.Name,
                native.Text,
                () => new RelicDefinition(native.DefinitionId, native.Name, native)));
        }

        foreach (var canonical in ModelDb.AllPotions.Where(model => !model.IsMock))
        {
            var potion = canonical.ToMutable();
            var native = new NativePotion(
                potion.Id.ToString(),
                potion.Title.GetFormattedText(),
                potion.DynamicDescription.GetFormattedText(),
                potion.TargetType.ToString().ToLowerInvariant(),
                potion.Usage.ToString().ToLowerInvariant());
            records.Add(new CatalogRecord(
                CatalogCategory.Potion,
                native.DefinitionId,
                native.Name,
                native.Text,
                () => new PotionDefinition(native.DefinitionId, native.Name, native)));
        }

        foreach (var canonical in ModelDb.All.OfType<OrbModel>().Where(model => !model.IsMock))
        {
            var orb = canonical.ToMutable(0);
            var native = new NativeOrb(
                orb.Id.ToString(),
                orb.Title.GetFormattedText(),
                orb.DumbHoverTip.Description);
            records.Add(new CatalogRecord(
                CatalogCategory.Orb,
                native.DefinitionId,
                native.Name,
                native.Text,
                () => new OrbDefinition(native.DefinitionId, native.Name, native)));
        }

        var characters = ModelDb.AllCharacters.ToArray();
        var pools = ModelDb.AllCardPools
            .Where(pool => !pool.IsMock)
            .Select(pool =>
            {
                var character = characters.FirstOrDefault(item => item.CardPool.Id == pool.Id);
                return new PoolFilter(pool.Id.ToString(), character?.Title.GetFormattedText() ?? pool.Title)
                {
                    Character = character is null
                        ? null
                        : new NamedDefinition(character.Title.GetFormattedText(), character.Id.ToString()),
                };
            })
            .OrderBy(pool => pool.Value, StringComparer.Ordinal)
            .ToImmutableArray();

        var types = cards
            .Select(card => card.Type)
            .Distinct()
            .Select(type => new NamedFilter(type.ToString().ToLowerInvariant(), type.ToLocString().GetFormattedText()))
            .OrderBy(value => value.Value, StringComparer.Ordinal)
            .ToImmutableArray();

        var rarities = cards
            .Select(card => card.Rarity)
            .Distinct()
            .Select(rarity => new NamedFilter(rarity.ToString().ToLowerInvariant(), rarity.ToLocString().GetFormattedText()))
            .OrderBy(value => value.Value, StringComparer.Ordinal)
            .ToImmutableArray();

        var costs = cards
            .Select(card => ResourceCost.FromNative(card.EnergyCost.Canonical, card.EnergyCost.CostsX))
            .Where(cost => cost.Kind is ResourceCostKind.Number or ResourceCostKind.X)
            .Distinct()
            .OrderBy(cost => cost.Kind)
            .ThenBy(cost => cost.Amount)
            .ToImmutableArray();

        return new CatalogPublication(
            new CatalogData(
                [CatalogCategory.Card, CatalogCategory.Relic, CatalogCategory.Potion, CatalogCategory.Orb],
                new CardFilterCatalog(pools, types, rarities, costs)),
            records.ToImmutable());
    }
}
