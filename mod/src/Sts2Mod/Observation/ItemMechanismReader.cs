using System.Collections.Concurrent;
using System.Globalization;
using System.Reflection;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Rooms;
using static Sts2Mod.Observation.NativeRuleBody;

namespace Sts2Mod.Observation;

internal static class ItemMechanismReader
{
    private const BindingFlags DeclaredInstance = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly;

    private sealed record CardCacheKey(string CardId, int UpgradeLevel, string Language);
    private sealed record SimpleCacheKey(string ModelId, string Language);

    private static readonly ConcurrentDictionary<CardCacheKey, string?> CardCache = new();
    private static readonly ConcurrentDictionary<SimpleCacheKey, string?> PotionCache = new();
    private static readonly ConcurrentDictionary<SimpleCacheKey, string?> RelicCache = new();
    private static readonly ConcurrentDictionary<Type, Type[]> CardPowerCache = new();
    private static readonly ConcurrentDictionary<Type, Type[]> PotionPowerCache = new();
    private static readonly ConcurrentDictionary<string, byte> LoggedGaps = new(StringComparer.Ordinal);

    public static Type[] ReferencedPowers(CardModel card)
        => ReferencedPowersFromMethod(card.GetType(), "OnPlay", CardPowerCache);

    public static Type[] ReferencedPowers(PotionModel potion)
        => ReferencedPowersFromMethod(potion.GetType(), "OnUse", PotionPowerCache);

    private static Type[] ReferencedPowersFromMethod(Type modelType, string methodName, ConcurrentDictionary<Type, Type[]> cache)
    {
        if (cache.TryGetValue(modelType, out var cached))
            return cached;

        var powers = new HashSet<Type>();
        try
        {
            if (modelType.GetMethod(methodName, DeclaredInstance) is { } method)
            {
                var body = NativeRuleBody.Read(method);
                foreach (var step in body.Calls)
                {
                    if (step.Call.Method.DeclaringType == typeof(PowerCmd)
                        && step.Call.Method.Name == nameof(PowerCmd.Apply)
                        && step.Call.Method.IsGenericMethod
                        && step.Call.Method.GetGenericArguments()[0] is { } pt
                        && typeof(PowerModel).IsAssignableFrom(pt)
                        && PowerMechanismReader.IsStandaloneReadable(pt))
                    {
                        powers.Add(pt);
                    }
                }
            }
        }
        catch
        {
            // Keep model observation resilient if a model method cannot be inspected.
        }

        var result = powers.OrderBy(t => t.FullName, StringComparer.Ordinal).ToArray();
        cache[modelType] = result;
        return result;
    }

    public static string? ReadCard(CardModel card, string language)
    {
        var key = new CardCacheKey(card.Id.ToString(), card.CurrentUpgradeLevel, language);
        if (CardCache.TryGetValue(key, out var cached))
            return cached;

        string? text = null;
        try
        {
            text = DescribeCard(card);
        }
        catch (Exception error)
        {
            var id = card.Id.ToString();
            if (LoggedGaps.TryAdd(id, 0))
                Godot.GD.PushWarning($"[sts2agent] Card mechanism unavailable for {id}: {error.Message}");
        }

        CardCache[key] = text;
        return text;
    }

    public static string? ReadPotion(PotionModel potion, string language)
    {
        var key = new SimpleCacheKey(potion.Id.ToString(), language);
        if (PotionCache.TryGetValue(key, out var cached))
            return cached;

        string? text = null;
        try
        {
            text = DescribePotion(potion);
        }
        catch (Exception error)
        {
            var id = potion.Id.ToString();
            if (LoggedGaps.TryAdd(id, 0))
                Godot.GD.PushWarning($"[sts2agent] Potion mechanism unavailable for {id}: {error.Message}");
        }

        PotionCache[key] = text;
        return text;
    }

    public static string? ReadRelic(RelicModel relic, string language)
    {
        var key = new SimpleCacheKey(relic.Id.ToString(), language);
        if (RelicCache.TryGetValue(key, out var cached))
            return cached;

        string? text = null;
        try
        {
            text = DescribeRelic(relic);
        }
        catch (Exception error)
        {
            var id = relic.Id.ToString();
            if (LoggedGaps.TryAdd(id, 0))
                Godot.GD.PushWarning($"[sts2agent] Relic mechanism unavailable for {id}: {error.Message}");
        }

        RelicCache[key] = text;
        return text;
    }

    internal static string? DescribeCard(CardModel card)
    {
        var cardType = card.GetType();
        if (card.Type is CardType.Status or CardType.Curse)
            return DescribeStatusOrCurseCard(card, cardType);

        var notes = new List<string>();

        if (cardType.GetMethod("OnPlay", DeclaredInstance) is { } onPlay)
        {
            var body = NativeRuleBody.Read(onPlay);
            if (!body.Incomplete)
            {
                if (DescribeCardHistoryPrecondition(cardType, body) is { } historyPrecondition)
                    notes.Add(historyPrecondition);

                if (DescribeCardFatalGuard(body) is { } fatalGuard)
                    notes.Add(fatalGuard);

                if (DescribeCardPlaySequence(cardType, body) is { } sequenceRule)
                    notes.Add(sequenceRule);

                if (DescribeCardCostLifetime(body) is { } costLifetime)
                    notes.Add(costLifetime);
            }
        }

        return notes.Count > 0 ? string.Join(" ", notes) : null;
    }

    private static string? DescribeStatusOrCurseCard(CardModel card, Type cardType)
    {
        var notes = new List<string>();

        if (cardType.GetMethod("get_ShouldPreventCardPlay", DeclaredInstance) is { } preventPlay
            && NativeRuleBody.Read(preventPlay) is { Incomplete: false } preventBody)
        {
            var limit = preventBody.Returns
                .SelectMany(r => r.Value is Operation { Left: Operation { Operator: "clt", Right: Constant { Data: int n } } } ? [n] : Array.Empty<int>())
                .FirstOrDefault();
            if (limit > 0)
                notes.Add($"While in hand, prevents owner from playing cards once {limit} card plays have started this turn (counting replays and autoplays).");
        }

        if (cardType.GetMethod("OnDrawn", DeclaredInstance) is { } onDrawn
            && NativeRuleBody.Read(onDrawn) is { Incomplete: false } drawnBody)
        {
            if (drawnBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(PlayerCmd) && s.Call.Method.Name == nameof(PlayerCmd.LoseEnergy)))
                notes.Add("When drawn into hand, immediately loses Energy (even if drawn mid-turn).");
        }

        if (cardType.GetMethod("OnTurnEndInHand", DeclaredInstance) is { } turnEnd
            && NativeRuleBody.Read(turnEnd) is { Incomplete: false } endBody)
        {
            if (endBody.Calls.FirstOrDefault(s => s.Call.Method.DeclaringType == typeof(PowerCmd)
                && s.Call.Method.Name == nameof(PowerCmd.Apply) && s.Call.Method.IsGenericMethod) is { } applyStep)
            {
                var pt = applyStep.Call.Method.GetGenericArguments()[0];
                var checksExisting = endBody.Calls.Any(s => s.Call.Method.Name == "HasPower" && s.Call.Method.IsGenericMethod
                    && s.Call.Method.GetGenericArguments()[0] == pt);
                var tickNote = checksExisting
                    ? " (if owner did not already have it, sets skip-next-tick so it persists through the next turn)"
                    : "";
                notes.Add($"If still in hand at end of turn, applies {NativeRuleText.ModelLabel(pt)} to owner{tickNote}.");
            }
            else if (endBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.Damage)))
            {
                var unblockable = endBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CreatureCmd)
                    && s.Call.Method.Name == nameof(CreatureCmd.Damage)
                    && s.Call.Arguments.Any(a => a is Constant { Data: int p } && ((MegaCrit.Sts2.Core.ValueProps.ValueProp)p).HasFlag(MegaCrit.Sts2.Core.ValueProps.ValueProp.Unblockable)));
                notes.Add(unblockable
                    ? "If still in hand at end of turn, deals unblockable unpowered damage to owner."
                    : "If still in hand at end of turn, deals unpowered damage (blockable by Block) to owner.");
            }
        }

        return notes.Count > 0 ? string.Join(" ", notes) : null;
    }

    private static string? DescribeCardCostLifetime(NativeRuleBody body)
    {
        if (body.Calls.Any(s => s.Call.Method.Name == "SetToFreeThisTurn"))
            return "Cost reduction sets target card(s) Energy cost to 0 until played or until the turn ends (whichever comes first).";
        if (body.Calls.Any(s => s.Call.Method.Name == "SetToFreeThisCombat"))
            return "Cost reduction sets target card(s) to 0 Energy for the rest of combat.";
        return null;
    }

    private static string? DescribeCardHistoryPrecondition(Type cardType, NativeRuleBody body)
    {
        foreach (var step in body.Calls)
        {
            foreach (var cond in step.Conditions)
            {
                if (cond.Value is not Call { Method: MethodInfo predGetter, Receiver: Input { Name: "this" }, Arguments.Length: 0 }
                    || predGetter.DeclaringType != cardType || predGetter.ReturnType != typeof(bool))
                {
                    continue;
                }

                var getterBody = NativeRuleBody.Read(predGetter);
                if (getterBody.Incomplete)
                    continue;

                foreach (var ret in getterBody.Returns)
                {
                    if (ret.Value is not Call { Method.Name: nameof(Enumerable.Any),
                            Arguments: [Call { Method: { Name: "OfType", IsGenericMethod: true } ofType },
                                Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo predMethod }] }] }
                        || ofType.GetGenericArguments()[0].Name != "CardExhaustedEntry")
                    {
                        continue;
                    }

                    var predBody = NativeRuleBody.Read(predMethod);
                    if (predBody.Incomplete)
                        continue;

                    var checksThisTurn = predBody.Calls.Any(c => c.Call.Method.Name == "HappenedThisTurn");
                    var checksOwner = predBody.Calls.Any(c => c.Call.Method.Name is "get_Owner" or "get_Actor");
                    if (checksThisTurn && checksOwner)
                        return "Bonus effect triggers only if at least one of owner's cards was exhausted earlier this turn.";
                }
            }
        }
        return null;
    }

    private static string? DescribeCardFatalGuard(NativeRuleBody body)
    {
        var checksFatalAllowed = body.Calls.Any(step => step.Conditions.Any(cond =>
            cond.Value is Call { Method.Name: nameof(Enumerable.All), Arguments: [Call { Method.Name: "get_Powers" },
                Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo pred }] }] }
            && NativeRuleBody.Read(pred) is { Incomplete: false, Returns: [{ Value: Call { Method.Name: nameof(PowerModel.ShouldOwnerDeathTriggerFatal) } }] }));
        var checksKilled = body.Calls.Any(step => step.Conditions.Any(cond =>
            cond.Value is Call { Method.Name: nameof(Enumerable.Any), Arguments: [_, Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo pred }] }] }
            && NativeRuleBody.Read(pred) is { Incomplete: false, Returns: [{ Value: Call { Method.Name: "get_WasTargetKilled" } }] }));
        if (!checksFatalAllowed || !checksKilled)
            return null;

        return $"Fatal bonus triggers only if this attack kills a target whose powers all allow Fatal (blocked by {NativeRuleText.ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Powers.MinionPower))} and non-fatal revives).";
    }

    private static string? DescribeCardPlaySequence(Type cardType, NativeRuleBody body)
    {
        var ordered = body.Calls.GroupBy(s => (s.Epoch, s.Index))
            .OrderBy(g => (g.Key.Epoch, g.Max(s => s.Order), g.Key.Index))
            .ToArray();

        // Combat-wide upgrade (e.g., Apotheosis): upgrades all upgradable cards in PlayerCombatState.AllCards excluding self.
        var upgradesAllCombatCards = ordered.Any(g => g.First().Call is { Method.Name: nameof(CardCmd.Upgrade) } upg
            && upg.Method.DeclaringType == typeof(CardCmd)
            && upg.Arguments.FirstOrDefault() is Call { Method.Name: "get_Current",
                Receiver: Call { Method.Name: "GetEnumerator", Receiver: Call { Method.Name: "get_AllCards" } } });
        if (upgradesAllCombatCards)
            return "Upgrades all upgradable cards across hand, draw, discard, exhaust, and play piles for the rest of combat (excluding itself).";

        var attackIdx = FindStepIndex(ordered, c => c.Method.Name == "Execute" && c.Method.DeclaringType?.Name == "AttackCommand");
        var blockIdx = FindStepIndex(ordered, c => c.Method.DeclaringType == typeof(CreatureCmd) && c.Method.Name == nameof(CreatureCmd.GainBlock));
        var drawIdx = FindStepIndex(ordered, c => c.Method.DeclaringType == typeof(CardPileCmd) && c.Method.Name == nameof(CardPileCmd.Draw));
        var selectHandIdx = FindStepIndex(ordered, c => c.Method.DeclaringType == typeof(CardSelectCmd) && c.Method.Name == nameof(CardSelectCmd.FromHand));
        var selectDiscardHandIdx = FindStepIndex(ordered, c => c.Method.DeclaringType == typeof(CardSelectCmd) && c.Method.Name == nameof(CardSelectCmd.FromHandForDiscard));
        var selectCombatPileIdx = FindStepIndex(ordered, c => c.Method.DeclaringType == typeof(CardSelectCmd) && c.Method.Name == nameof(CardSelectCmd.FromCombatPile));
        var exhaustIdx = FindStepIndex(ordered, c => c.Method.DeclaringType == typeof(CardCmd) && c.Method.Name == nameof(CardCmd.Exhaust));
        var pileAddIdx = FindStepIndex(ordered, c => c.Method.DeclaringType == typeof(CardPileCmd) && c.Method.Name == nameof(CardPileCmd.Add));

        // Attack -> filtered pile retrieval (e.g., AllForOne: Attack first, then move 0-cost non-X Attack/Skill/Power cards from discard to hand).
        if (attackIdx >= 0 && pileAddIdx > attackIdx && selectCombatPileIdx < 0)
        {
            var addCall = ordered[pileAddIdx].First().Call;
            if (addCall.Arguments.ElementAtOrDefault(1) is Constant { Data: (int)PileType.Hand }
                && cardType.GetMethod("Filter", DeclaredInstance) is { } filterMethod
                && IsZeroCostPlayableFilter(filterMethod))
            {
                return "Resolves attack damage first, then moves all 0-cost non-X Attack, Skill, and Power cards from discard pile into hand.";
            }
        }

        // Attack or Block -> select from combat pile -> move to target pile (e.g., Headbutt, Hologram, ArrowStab).
        if ((attackIdx >= 0 || blockIdx >= 0) && selectCombatPileIdx > Math.Max(attackIdx, blockIdx) && pileAddIdx > selectCombatPileIdx)
        {
            var firstAction = attackIdx >= 0 ? "Resolves attack damage first" : "Gains Block first";
            var selGroup = ordered[selectCombatPileIdx];
            var selCall = selGroup.First().Call;
            var addCall = ordered[pileAddIdx].First().Call;
            var srcPile = ExtractPileType(selCall.Arguments.ElementAtOrDefault(1));
            var dstPile = addCall.Arguments.ElementAtOrDefault(1) is Constant { Data: int rawDst } ? (PileType?)rawDst : null;
            var dstPos = addCall.Arguments.ElementAtOrDefault(2) is Constant { Data: int rawPos } ? (CardPilePosition)rawPos : CardPilePosition.Bottom;
            if (srcPile is not null && dstPile is not null)
            {
                var conditionClause = DescribeCombatPileSelectionCondition(body, selGroup, out var condValid);
                var filterClause = DescribeCombatPileSelectionFilter(selCall, out var filterValid);
                if (!condValid || !filterValid)
                    return null;

                var dstWhere = dstPile == PileType.Draw && dstPos == CardPilePosition.Top
                    ? "on top of draw pile"
                    : dstPile == PileType.Hand
                        ? "into hand"
                        : $"into {dstPile.Value.ToString().ToLowerInvariant()} pile";
                var cardDesc = filterClause is not null ? $"{filterClause} card(s)" : "card(s)";
                return conditionClause is not null
                    ? $"{firstAction}; {conditionClause}, selects {cardDesc} from {srcPile.Value.ToString().ToLowerInvariant()} pile to move {dstWhere}."
                    : $"{firstAction}, then selects {cardDesc} from {srcPile.Value.ToString().ToLowerInvariant()} pile to move {dstWhere}.";
            }
        }

        // Draw -> select from hand to discard (e.g., Acrobatics, Prepared).
        if (drawIdx >= 0 && selectDiscardHandIdx > drawIdx)
        {
            var noDrawLabel = NativeRuleText.ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Powers.NoDrawPower));
            return $"Draws cards first (blocked by {noDrawLabel}), then selects from the resulting hand to discard.";
        }

        // Select from hand to exhaust -> Draw (e.g., BurningPact): check whether Draw is gated on selecting an eligible card!
        if (selectHandIdx >= 0 && exhaustIdx > selectHandIdx && drawIdx > exhaustIdx)
        {
            var drawGroupKey = ordered[drawIdx].Key;
            var drawVisitedAlways = body.Terminals.All(t => t.Visited.Contains(drawGroupKey));
            return drawVisitedAlways
                ? "Selects card(s) in hand to exhaust first, then draws cards even if no other card was in hand to exhaust."
                : "Selects card(s) in hand to exhaust first; draws cards only if a card was selected and exhausted.";
        }

        // Discard entire hand and draw (either equal to discarded count as in CalculatedGamble, or a fixed count as in WheelOfFortune).
        var discardAndDrawIdx = FindStepIndex(ordered, c => c.Method.DeclaringType == typeof(CardCmd) && c.Method.Name == nameof(CardCmd.DiscardAndDraw));
        if (discardAndDrawIdx >= 0)
        {
            var ddCall = ordered[discardAndDrawIdx].First().Call;
            if (ddCall.Arguments.ElementAtOrDefault(1) is Call { Method.Name: "get_Cards", Receiver: Call { Method.Name: "GetPile", Arguments: [Constant { Data: (int)PileType.Hand }, ..] } })
            {
                var noDrawLabel = NativeRuleText.ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Powers.NoDrawPower));
                var drawCountArg = ddCall.Arguments.ElementAtOrDefault(2);
                if (drawCountArg is Call { Method.Name: nameof(Enumerable.Count) or "get_Count" })
                    return $"Counts and discards all other cards currently in hand first, then draws that many cards (draw step is blocked by {noDrawLabel}).";
                if (drawCountArg is Call { Method.Name: "get_IntValue" or "get_BaseValue" })
                    return $"Discards all other cards currently in hand first, then draws its fixed draw count even if no other cards were in hand (draw step is blocked by {noDrawLabel}).";
            }
        }

        return null;
    }

    private static bool IsZeroCostPlayableFilter(MethodInfo filterMethod)
    {
        var body = NativeRuleBody.Read(filterMethod);
        if (body.Incomplete)
            return false;
        var checksZeroCost = body.Returns.Any(r => r.Conditions.Any(c => c.Value is Call { Method.Name: "GetWithModifiers" } && !c.Expected));
        var checksNotX = body.Returns.Any(r => r.Conditions.Any(c => c.Value is Operation { Left: Call { Method.Name: "get_CostsX" } })
            || r.Value is Operation { Left: Call { Method.Name: "get_CostsX" } });
        return checksZeroCost && checksNotX;
    }

    private static string? DescribeCombatPileSelectionCondition(
        NativeRuleBody body,
        IGrouping<(int Epoch, int Index), Step> selGroup,
        out bool valid)
    {
        valid = true;
        if (body.Terminals.Count > 0 && body.Terminals.All(t => t.Visited.Contains(selGroup.Key)))
            return null;

        var clauses = new List<string>();
        foreach (var cond in selGroup.First().Conditions)
        {
            if (cond is { Expected: true, Value: Call { Method.Name: "PlayerHasAnyArrows" } })
            {
                if (!clauses.Contains("owner has at least 1 arrow", StringComparer.Ordinal))
                    clauses.Add("owner has at least 1 arrow");
            }
            else if (cond.Value is Operation { Operator: "clt" or "blt" or "bge",
                Left: Call { Method.Name: "get_Count", Receiver: Call { Method.Name: "get_Cards" } },
                Right: Constant { Data: int } or Call { Method.Name: "get_MaxCardsInHand" } } handOp
                && ((handOp.Operator == "bge" && !cond.Expected) || (handOp.Operator != "bge" && cond.Expected)))
            {
                if (!clauses.Contains("hand is not full", StringComparer.Ordinal))
                    clauses.Add("hand is not full");
            }
            else
            {
                valid = false;
                return null;
            }
        }
        return clauses.Count > 0 ? $"if {string.Join(" and ", clauses)}" : null;
    }

    private static string? DescribeCombatPileSelectionFilter(Call selCall, out bool valid)
    {
        valid = true;
        var filterParam = selCall.Method.GetParameters().FirstOrDefault(p => p.ParameterType == typeof(Func<CardModel, bool>));
        if (filterParam is null || filterParam.Position >= selCall.Arguments.Length)
            return null;
        var arg = selCall.Arguments[filterParam.Position];
        if (arg is Constant { Data: null })
            return null;
        if (arg is not Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo predMethod }] })
        {
            valid = false;
            return null;
        }
        var predBody = NativeRuleBody.Read(predMethod);
        if (predBody.Incomplete)
        {
            valid = false;
            return null;
        }
        var tags = predBody.Calls
            .Where(s => s.Call.Method.Name == "Contains" && s.Call.Arguments.OfType<Constant>().Any(c => c.Data is int raw && Enum.IsDefined(typeof(CardTag), raw)))
            .SelectMany(s => s.Call.Arguments.OfType<Constant>().Where(c => c.Data is int raw && Enum.IsDefined(typeof(CardTag), raw)).Select(c => ((CardTag)(int)c.Data!).ToString()))
            .Distinct(StringComparer.Ordinal)
            .ToArray();
        if (tags.Length > 0)
            return string.Join("/", tags);
        valid = false;
        return null;
    }

    private static PileType? ExtractPileType(Value? expr)
        => expr is Call { Method.Name: "GetPile", Arguments: [Constant { Data: int rawPile }, ..] }
            ? (PileType)rawPile
            : null;

    private static int FindStepIndex(IGrouping<(int Epoch, int Index), Step>[] groups, Func<Call, bool> predicate)
    {
        for (var i = 0; i < groups.Length; i++)
        {
            if (groups[i].Any(s => predicate(s.Call)))
                return i;
        }
        return -1;
    }

    internal static string? DescribePotion(PotionModel potion)
    {
        var type = potion.GetType();
        var notes = new List<string>();

        var cannotGenerate = type.GetMethod("get_CanBeGeneratedInCombat", DeclaredInstance) is { } canGenMethod
            && NativeRuleBody.Read(canGenMethod) is { Incomplete: false, Returns: [{ Conditions.Length: 0, Value: var v }] }
            && TryTruth(v, out var allowed) && !allowed;

        if (type.GetMethod("ShouldDie", DeclaredInstance) is { } shouldDie
            && NativeRuleBody.Read(shouldDie) is { Incomplete: false } dieBody
            && dieBody.Returns.Any(r => TryTruth(r.Value, out var dies) && !dies))
        {
            var genClause = cannotGenerate ? "; cannot be randomly generated in combat" : "";
            return $"Automatic death prevention: triggers before late-stage death-prevention relics when owner would die (unless death prevention is bypassed), consuming this potion{genClause}.";
        }

        if (type.GetMethod("OnUse", DeclaredInstance) is { } onUse)
        {
            var body = NativeRuleBody.Read(onUse);
            if (!body.Incomplete)
            {
                var procsRandomOutOfCombat = body.Calls.Any(s => s.Call.Method.Name == "CreateRandomPotionOutOfCombat");
                if (procsRandomOutOfCombat)
                    notes.Add("Fills each currently empty potion slot with a random potion from the non-combat potion pool.");

                var upgradesCreatedInHand = body.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CardCmd)
                    && s.Call.Method.Name == nameof(CardCmd.Upgrade)
                    && ContainsMethodName(s.Call.Arguments.FirstOrDefault(), "CreateInHand"));
                if (upgradesCreatedInHand)
                    notes.Add("Creates cards in hand first, then upgrades each created card.");

                var ordered = body.Calls.GroupBy(s => (s.Epoch, s.Index))
                    .OrderBy(g => (g.Key.Epoch, g.Max(s => s.Order), g.Key.Index))
                    .ToArray();
                var setsFreeThisTurn = ordered.Any(g => g.Any(s => s.Call.Method.Name == "SetToFreeThisTurn"));
                var selectsDiscard = ordered.Any(g => g.Any(s => s.Call.Method.DeclaringType == typeof(CardSelectCmd)
                    && s.Call.Method.Name == nameof(CardSelectCmd.FromCombatPile)
                    && ExtractPileType(s.Call.Arguments.ElementAtOrDefault(1)) == PileType.Discard));
                if (selectsDiscard && setsFreeThisTurn)
                    notes.Add("Selects from discard pile, sets Energy cost to 0 until played or until the turn ends (whichever comes first), and moves into hand.");
            }
            else if (CallsOnlyRandomPotionFill(onUse))
            {
                notes.Add("Fills each currently empty potion slot with a random potion from the non-combat potion pool.");
            }
        }

        return notes.Count > 0 ? string.Join(" ", notes) : null;
    }

    internal static string? DescribeRelic(RelicModel relic)
    {
        var type = relic.GetType();
        var notes = new List<string>();

        // Unknown map room type filter (e.g., JuzuBracelet).
        if (type.GetMethod("ModifyUnknownMapPointRoomTypes", DeclaredInstance) is { } unknownHook
            && NativeRuleBody.Read(unknownHook) is { Incomplete: false } unknownBody)
        {
            var removed = unknownBody.Calls
                .Where(s => s.Call.Method.Name == "Remove" && s.Call.Arguments is [Constant { Data: int rt }] && Enum.IsDefined(typeof(RoomType), rt))
                .Select(s => ((RoomType)(int)((Constant)s.Call.Arguments[0]).Data!).ToString())
                .Distinct(StringComparer.Ordinal)
                .ToArray();
            if (removed.Length > 0)
                notes.Add($"Unknown (?) map nodes exclude room types: {string.Join(", ", removed)}.");
        }

        // Rest site heal modifier (e.g., RegalPillow).
        if (type.GetMethod("ModifyRestSiteHealAmount", DeclaredInstance) is { } restHealHook
            && NativeRuleBody.Read(restHealHook) is { Incomplete: false } restBody
            && relic.DynamicVars.TryGetValue("Heal", out var pillowHeal))
        {
            var addsHeal = restBody.Returns.Any(r => r.Value is Call { Method.Name: "op_Addition" });
            if (addsHeal)
                notes.Add($"Rest site Rest option heals +{pillowHeal.BaseValue.ToString("0.########", CultureInfo.InvariantCulture)} additional HP (applies to owner and owner's pet).");
        }

        // Late death prevention (e.g., LizardTail).
        if (type.GetMethod("ShouldDieLate", DeclaredInstance) is { } dieLate
            && type.GetMethod("AfterPreventingDeath", DeclaredInstance) is { } afterPrevent
            && NativeRuleBody.Read(dieLate) is { Incomplete: false } dieLateBody
            && NativeRuleBody.Read(afterPrevent) is { Incomplete: false } preventBody
            && dieLateBody.Returns.Any(r => TryTruth(r.Value, out var d) && !d)
            && preventBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.Heal))
            && relic.DynamicVars.TryGetValue("Heal", out var hv))
        {
            var pct = hv.BaseValue.ToString("0.########", CultureInfo.InvariantCulture);
            notes.Add($"Late death prevention: checked after earlier death-prevention effects (such as {NativeRuleText.ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Potions.FairyInABottle))}); once per run, prevents owner death and heals max(1, floor({pct}% of Max HP)).");
        }

        // Post-Osty flat HP loss reduction (e.g., TungstenRod).
        if (type.GetMethod("ModifyHpLostAfterOsty", DeclaredInstance) is { } hpLossHook
            && NativeRuleBody.Read(hpLossHook) is { Incomplete: false } hpLossBody
            && hpLossBody.Returns.Any(r => r.Value is Call { Method.Name: nameof(Math.Max), Arguments: [_, Call { Method.Name: "op_Subtraction" }] })
            && relic.DynamicVars.TryGetValue("HpLossReduction", out var redVar))
        {
            notes.Add($"Reduces each unblocked HP loss on owner (after {NativeRuleText.ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty))} redirection) by {redVar.BaseValue.ToString("0.########", CultureInfo.InvariantCulture)} (minimum 0).");
        }

        // Boss-entry one-time full heal (e.g., BathRobes).
        if (type.GetMethod("BeforeCombatStart", DeclaredInstance) is { } beforeCombat
            && type.GetProperty("IsUsedUp", DeclaredInstance) is not null
            && NativeRuleBody.Read(beforeCombat) is { Incomplete: false } bcBody
            && bcBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.Heal)
                && s.Conditions.Any(c => c.Value is Operation { Left: Call { Method.Name: "get_RoomType" }, Right: Constant { Data: (int)RoomType.Boss } })))
        {
            notes.Add("Triggers before Boss combat starts (once per run if not yet used): heals owner to full HP and becomes used up.");
        }

        // Doom fatal heal (e.g., BookRepairKnife).
        if (type.GetMethod("AfterDiedToDoom", DeclaredInstance) is { } doomHook
            && NativeRuleBody.Read(doomHook) is { Incomplete: false } doomBody
            && doomBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.Heal))
            && relic.DynamicVars.TryGetValue("Heal", out var doomHeal))
        {
            notes.Add($"Heals {doomHeal.BaseValue.ToString("0.########", CultureInfo.InvariantCulture)} HP per enemy killed by {NativeRuleText.ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Powers.DoomPower))} whose powers all allow Fatal (ignores {NativeRuleText.ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Powers.MinionPower))} enemies).");
        }

        // Self-turn first unblocked HP loss heal (e.g., DemonTongue).
        if (type.GetMethod("AfterDamageReceived", DeclaredInstance) is { } dmgRec
            && NativeRuleBody.Read(dmgRec) is { Incomplete: false } dmgRecBody
            && dmgRecBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.Heal)
                && s.Conditions.Any(c => c.Value is Operation { Left: Call { Method.Name: "get_CurrentSide" } })))
        {
            notes.Add("Once during each of owner's turns, the first time owner loses unblocked HP on owner's turn, heals HP equal to that unblocked damage.");
        }

        // First exhausted Skill clone per combat (e.g., BurningSticks).
        if (type.GetMethod("AfterCardExhausted", DeclaredInstance) is { } cardExh
            && NativeRuleBody.Read(cardExh) is { Incomplete: false } cardExhBody
            && cardExhBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CardPileCmd)
                && s.Call.Method.Name == nameof(CardPileCmd.AddGeneratedCardToCombat)
                && ContainsMethodName(s.Call.Arguments.FirstOrDefault(), "CreateClone")))
        {
            notes.Add("Once per combat, when an owned Skill card is exhausted (including Ethereal), adds a clone of that card into hand.");
        }

        // Energy + Star cost zeroing on Nth non-autoplay card each turn (e.g., BrilliantScarf).
        if (type.GetMethod("TryModifyEnergyCostInCombatLate", DeclaredInstance) is { } scarfEnergy
            && type.GetMethod("TryModifyStarCost", DeclaredInstance) is { } scarfStar
            && type.GetMethod("AfterCardPlayed", DeclaredInstance) is { } scarfPlayed
            && NativeRuleBody.Read(scarfEnergy) is { Incomplete: false }
            && NativeRuleBody.Read(scarfStar) is { Incomplete: false }
            && NativeRuleBody.Read(scarfPlayed) is { Incomplete: false } scarfPlayedBody
            && scarfPlayedBody.Returns.Any(r => r.Conditions.Any(c => c.Value is Call { Method.Name: "get_IsAutoPlay" }))
            && relic.DynamicVars.TryGetValue("Cards", out var scarfCards))
        {
            var threshold = (scarfCards.BaseValue - 1m).ToString("0.########", CultureInfo.InvariantCulture);
            notes.Add($"Counts non-autoplay cards played by owner this turn; while counter equals {threshold}, sets both Energy and Star costs of owner's hand/play cards to 0.");
        }

        // Attack-free turn Energy carry-over (e.g., ArtOfWar).
        if (type.GetMethod("AfterEnergyReset", DeclaredInstance) is { } energyReset
            && type.GetMethod("AfterSideTurnEnd", DeclaredInstance) is { } artEnd
            && type.GetMethod("AfterCardPlayed", DeclaredInstance) is { } artPlayed
            && NativeRuleBody.Read(energyReset) is { Incomplete: false } erBody
            && NativeRuleBody.Read(artEnd) is { Incomplete: false }
            && NativeRuleBody.Read(artPlayed) is { Incomplete: false }
            && erBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(PlayerCmd) && s.Call.Method.Name == nameof(PlayerCmd.GainEnergy)
                && s.Conditions.Any(c => c.Value is Call { Method.Name: "get_AnyAttacksPlayedLastTurn" })))
        {
            var energyAmt = relic.DynamicVars.TryGetValue("Energy", out var enVar)
                ? $"{enVar.BaseValue.ToString("0.########", CultureInfo.InvariantCulture)} Energy"
                : "its Energy";
            notes.Add($"Tracks whether owner played any Attack card during owner's turn; grants {energyAmt} after turn-start Energy reset only if owner completed their previous turn with 0 Attacks played.");
        }

        // Nth Attack damage doubling across multi-hit and Osty (e.g., PenNib).
        if (type.GetMethod("ModifyDamageMultiplicative", DeclaredInstance) is { } penMult
            && type.GetMethod("NotifyAttackPlayed", DeclaredInstance) is { } notifyAttack
            && type.GetProperty("AttackToDouble", DeclaredInstance) is not null
            && NativeRuleBody.Read(penMult) is { Incomplete: false }
            && NativeRuleBody.Read(notifyAttack) is { Incomplete: false } notifyBody
            && ExtractCounterModulus(relic, notifyBody) is { } mod && mod > 1)
        {
            notes.Add($"Counter advances mod {mod} on each owned Attack card play (persisting across combats); when counter reaches {mod - 1} before the next Attack, doubles powered card attack damage dealt by owner or {NativeRuleText.ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty))} across all hits of that Attack card.");
        }

        // Very-early zero-block check before turn-end block gain (e.g., Orichalcum, FakeOrichalcum).
        if (type.GetMethod("BeforeSideTurnEndVeryEarly", DeclaredInstance) is { } veryEarly
            && type.GetMethod("BeforeSideTurnEnd", DeclaredInstance) is { } beforeEnd
            && NativeRuleBody.Read(veryEarly) is { Incomplete: false }
            && NativeRuleBody.Read(beforeEnd) is { Incomplete: false } beBody
            && beBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.GainBlock)))
        {
            var blockAmt = relic.DynamicVars.TryGetValue("Block", out var blkVar)
                ? $"{blkVar.BaseValue.ToString("0.########", CultureInfo.InvariantCulture)} Block"
                : "its Block";
            notes.Add($"Checks whether owner has 0 Block very early at end of turn (before Plating and other early turn-end Block effects), and if so grants {blockAmt} before turn end.");
        }

        // Empty-hand draw phase restriction (e.g., UnceasingTop).
        if (type.GetMethod("AfterHandEmptied", DeclaredInstance) is { } emptyHand
            && NativeRuleBody.Read(emptyHand) is { Incomplete: false } ehBody
            && ehBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CardPileCmd) && s.Call.Method.Name == nameof(CardPileCmd.Draw)))
        {
            notes.Add("Draws 1 card when hand becomes empty during owner's active turn phases (does not trigger during end-of-turn discard).");
        }

        return notes.Count > 0 ? string.Join(" ", notes) : null;
    }

    private static bool CallsOnlyRandomPotionFill(MethodInfo onUse)
    {
        var asyncAttr = onUse.GetCustomAttribute<System.Runtime.CompilerServices.AsyncStateMachineAttribute>();
        var target = asyncAttr?.StateMachineType.GetMethod("MoveNext", DeclaredInstance) ?? onUse;
        var instructions = HarmonyLib.PatchProcessor.GetOriginalInstructions(target);
        var sawRandomOutOfCombat = false;
        foreach (var inst in instructions)
        {
            if (inst.operand is not MethodBase called)
                continue;
            if (called.Name == "CreateRandomPotionOutOfCombat")
            {
                sawRandomOutOfCombat = true;
                continue;
            }
            var decl = called.DeclaringType;
            if (decl is not null && decl.Namespace == typeof(CreatureCmd).Namespace && decl != typeof(PotionCmd))
                return false;
        }
        return sawRandomOutOfCombat;
    }

    private static int? ExtractCounterModulus(RelicModel relic, NativeRuleBody notifyBody)
    {
        foreach (var step in notifyBody.Calls)
        {
            if (step.Call.Method.Name != "set_AttacksPlayed")
                continue;
            if (step.Call.Arguments is [Operation { Operator: "rem", Right: Constant { Data: int mod } }])
                return mod;
            if (step.Call.Method is MethodInfo setter && NativeRuleBody.Read(setter) is { Incomplete: false } setterBody)
            {
                foreach (var fs in setterBody.FieldSteps)
                    if (fs.Assignment.Assigned is Operation { Operator: "rem", Right: Constant { Data: int setterMod } })
                        return setterMod;
            }
            if (relic.DynamicVars.TryGetValue("Cards", out var cardsVar) && cardsVar.BaseValue > 1m)
                return (int)cardsVar.BaseValue;
        }
        return null;
    }

    private static bool ContainsMethodName(Value? value, string methodName) => value switch
    {
        null => false,
        Call call => call.Method.Name == methodName
            || ContainsMethodName(call.Receiver, methodName)
            || call.Arguments.Any(a => ContainsMethodName(a, methodName)),
        Operation op => ContainsMethodName(op.Left, methodName) || ContainsMethodName(op.Right, methodName),
        _ => false,
    };
}
