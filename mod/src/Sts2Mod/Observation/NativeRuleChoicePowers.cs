using System.Reflection;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Powers;
using MegaCrit.Sts2.Core.Models;
using static Sts2Mod.Observation.NativeRuleBody;

namespace Sts2Mod.Observation;

internal sealed partial class NativeRuleText
{
    [ThreadStatic]
    private static HashSet<Type>? _activeChoicePowerProbes;

    private bool HasReadableChoicePower(Type power)
    {
        _activeChoicePowerProbes ??= [];
        if (!_activeChoicePowerProbes.Add(power))
            return false;
        try
        {
            var probe = new NativeRuleText(monster);
            var lines = new List<string>();
            probe.ReadHooks(power, lines);
            return lines.Count > 0 && probe.Gaps.Count == 0;
        }
        finally
        {
            _activeChoicePowerProbes.Remove(power);
        }
    }

    private void ReadPlayerResourceHooks(Type type, string powerPrefix, HashSet<string> handled, List<string> lines)
    {
        if (FindOverride(type, "ModifyHandDraw") is { } drawHook)
        {
            handled.Add(drawHook.Name);
            if (ReadPlayerDeltaHook(type, drawHook, "count", "Turn-start card draw") is { } drawLine)
                lines.Add($"{powerPrefix}{drawLine}");
            else
                Gap($"{type.Name}.{drawHook.Name}");
        }
        if (FindOverride(type, "ModifyMaxEnergy") is { } energyHook)
        {
            handled.Add(energyHook.Name);
            if (ReadPlayerDeltaHook(type, energyHook, "amount", "Maximum Energy") is { } energyLine)
                lines.Add($"{powerPrefix}{energyLine}");
            else
                Gap($"{type.Name}.{energyHook.Name}");
        }
        if (FindOverride(type, "ModifyOrbValue") is { } orbValueHook)
        {
            handled.Add(orbValueHook.Name);
            var focusLabel = ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Powers.FocusPower));
            var lightningLabel = ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Orbs.LightningOrb));
            var frostLabel = ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Orbs.FrostOrb));
            var glassLabel = ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Orbs.GlassOrb));
            var darkLabel = ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Orbs.DarkOrb));
            var plasmaLabel = ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Orbs.PlasmaOrb));
            var orbValueSubject = $"{focusLabel}-affected orb values (applies to orb stats scaled by {focusLabel}: {lightningLabel}/{frostLabel} passive and evoke, {glassLabel} passive and 2× evoke, {darkLabel} per-turn growth only; {plasmaLabel} is unaffected)";
            if (ReadPlayerDeltaHook(type, orbValueHook, "value", orbValueSubject) is { } orbValueLine)
                lines.Add($"{powerPrefix}{orbValueLine}");
            else
                Gap($"{type.Name}.{orbValueHook.Name}");
        }
        if (FindOverride(type, "ModifyOrbPassiveTriggerCounts") is { } orbTriggerHook)
        {
            handled.Add(orbTriggerHook.Name);
            if (ReadOrbPassiveTriggerCountHook(type, orbTriggerHook, handled) is { } orbTriggerLine)
                lines.Add($"{powerPrefix}{orbTriggerLine}");
            else
                Gap($"{type.Name}.{orbTriggerHook.Name}");
        }
        if (HasCardPlayLimit(type))
        {
            handled.Add("BeforeCardPlayed");
            handled.Add("BeforeSideTurnStart");
            handled.Add("ShouldPlay");
            lines.Add($"{powerPrefix}Card play limit: prevent owner from starting another card play once the turn's play count (including replays and autoplay) reaches stacks; resets before owner's turn starts");
        }
        foreach (var costHookName in new[] { "TryModifyEnergyCostInCombat", "TryModifyEnergyCostInCombatLate" })
        {
            if (handled.Contains(costHookName) || FindOverride(type, costHookName) is not { } costHook)
                continue;
            handled.Add(costHook.Name);
            if (ReadEnergyCostHook(costHook) is { } costLine)
                lines.Add($"{powerPrefix}{costLine}");
            else
                Gap($"{type.Name}.{costHook.Name}");
        }
        if (!handled.Contains("ModifyCardPlayCount") && FindOverride(type, "ModifyCardPlayCount") is { } playCountHook)
        {
            handled.Add(playCountHook.Name);
            if (ReadCardPlayCountHook(type, playCountHook, handled) is { } playCountLine)
                lines.Add($"{powerPrefix}{playCountLine}");
            else
                Gap($"{type.Name}.{playCountHook.Name}");
        }
        if (!handled.Contains("ModifyCardPlayResultLocation") && FindOverride(type, "ModifyCardPlayResultLocation") is { } playLocHook)
        {
            if (ReadCardPlayResultLocationHook(type, playLocHook, handled) is { } playLocLine)
            {
                handled.Add(playLocHook.Name);
                lines.Add($"{powerPrefix}{playLocLine}");
            }
            else if (FindOverride(type, "AfterModifyingCardPlayResultLocation") is null || !HasInternalDataFieldAccess(playLocHook))
            {
                handled.Add(playLocHook.Name);
                Gap($"{type.Name}.{playLocHook.Name}");
            }
        }
        if (FindOverride(type, "ModifyPreBlockDamageTarget") is { } preBlockTargetHook)
        {
            handled.Add(preBlockTargetHook.Name);
            if (ReadDamageTargetRedirectHook(preBlockTargetHook, "Damage target redirection (before Block)", "redirect damage to owner first; any overkill continues to original target (through Block and any remaining damage redirection)") is { } redirectLine)
                lines.Add($"{powerPrefix}{redirectLine}");
            else
                Gap($"{type.Name}.{preBlockTargetHook.Name}");
        }
        if (FindOverride(type, "ModifyUnblockedDamageTarget") is { } unblockedTargetHook)
        {
            handled.Add(unblockedTargetHook.Name);
            if (ReadDamageTargetRedirectHook(unblockedTargetHook, "Damage target redirection (after Block, unblocked damage)", "redirect unblocked damage to owner first; any overkill continues to original target") is { } redirectLine)
                lines.Add($"{powerPrefix}{redirectLine}");
            else
                Gap($"{type.Name}.{unblockedTargetHook.Name}");
        }
    }

    private string? ReadDamageTargetRedirectHook(MethodInfo hook, string label, string redirectText)
    {
        var body = Body(hook);
        if (body.Incomplete || body.Returns.Count == 0)
            return null;
        var outcomes = new List<(string Text, Test[] Conditions)>();
        foreach (var result in body.Returns)
        {
            // Skip impossible paths where a non-null incoming damage target equals null (from `Owner.PetOwner?.Creature`).
            if (result.Conditions.Any(c => IsEquality(c, true, out var l, out var r)
                && ((l is Input { Name: "target" or "originalTarget" } && r is Constant { Data: null })
                    || (r is Input { Name: "target" or "originalTarget" } && l is Constant { Data: null }))))
            {
                continue;
            }
            if (result.Value is Input { Name: "target" })
                outcomes.Add(("", result.Conditions));
            else if (IsOwner(result.Value))
                outcomes.Add((redirectText, result.Conditions));
            else
                return null;
        }
        var rendered = RenderOutcomes(outcomes, body.Terminals.Where(t => !t.Returned).Select(t => t.Conditions));
        return rendered is { Count: > 0 } ? $"{label}: {string.Join("; ", rendered)}" : null;
    }

    private string? ReadEnergyCostHook(MethodInfo hook)
    {
        var body = Body(hook);
        if (body.Incomplete)
            return null;
        var nonFalse = body.Returns.Where(r => !(TryTruth(r.Value, out var m) && !m)).ToArray();
        if (nonFalse.Length == 0 || nonFalse.Any(r => !(TryTruth(r.Value, out var b) && b)))
            return null;
        var active = nonFalse;
        var allModifiedCostStores = body.OutStores.Where(s => s.Parameter.Name == "modifiedCost").ToArray();
        if (allModifiedCostStores.Length == 0)
            return null;

        // Every reachable active return path must have its final implied OutStore agree on the same cost rewrite
        // (including passthrough restores such as `modifiedCost = originalCost`, which must NOT be skipped).
        var activeStores = active
            .Where(r => !IsUnreachableNullablePath(r.Conditions))
            .Select(r => allModifiedCostStores.LastOrDefault(s => ConditionsImply(r.Conditions, s.Conditions)))
            .ToArray();
        if (activeStores.Length == 0 || activeStores.Any(s => s is null))
            return null;

        var setsZero = activeStores.All(s => Numeric(s!.Assigned) == 0m);
        var addsStacks = !setsZero && activeStores.All(s =>
            s!.Assigned is Call { Method.Name: "op_Addition", Arguments: [Input { Name: "originalCost" }, var delta] }
            && IsStackAmount(delta));
        if (!setsZero && !addsStacks)
            return null;

        CardType? filteredType = null;
        var requiredKeywords = new List<CardKeyword>();
        Type? requiredAffliction = null;
        var allowedPiles = new List<PileType>();
        var excludedPilesPerPath = new List<(PileType? PositivePile, List<PileType> ExcludedPiles)>();
        var firstPath = true;

        var reachablePaths = 0;

        foreach (var ret in active)
        {
            if (IsUnreachableNullablePath(ret.Conditions))
                continue;

            var hasOwnerCheck = false;
            CardType? pathType = null;
            var pathKeywords = new List<CardKeyword>();
            Type? pathAffliction = null;
            PileType? pathPile = null;
            var pathExcludedPiles = new List<PileType>();

            foreach (var rawCond in ret.Conditions)
            {
                var condExpr = rawCond.Value;
                var condExpected = rawCond.Expected;
                while (condExpr is Operation { Operator: "ceq", Left: var inner, Right: Constant { Data: 0 or null } })
                {
                    condExpr = inner;
                    condExpected = !condExpected;
                }

                // Nullable<T>.HasValue on a constructed Nullable<T> is always true (and false on default).
                if (condExpr is Call { Method.Name: "get_HasValue", Method.DeclaringType: { IsGenericType: true } nullableDecl } hvCall
                    && nullableDecl.GetGenericTypeDefinition() == typeof(Nullable<>))
                {
                    if ((condExpected && hvCall.Receiver is Call { Method: ConstructorInfo })
                        || (!condExpected && hvCall.Receiver is Constant { Data: 0 }))
                        continue;
                    return null;
                }

                // Owner check: card.Owner.Creature == base.Owner or card.Owner == base.Owner.Player or card.Owner == base.Owner
                if (IsEquality(new Test(condExpr, condExpected), true, out var eqL, out var eqR)
                    && ((eqL is Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_Owner", Receiver: Input { Name: "card" } } } && IsOwner(eqR))
                        || (eqR is Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_Owner", Receiver: Input { Name: "card" } } } && IsOwner(eqL))
                        || (eqL is Call { Method.Name: "get_Owner", Receiver: Input { Name: "card" } }
                            && (IsOwner(eqR) || eqR is Call { Method.Name: "get_Player", Receiver: var pRecL } && IsOwner(pRecL)))
                        || (eqR is Call { Method.Name: "get_Owner", Receiver: Input { Name: "card" } }
                            && (IsOwner(eqL) || eqL is Call { Method.Name: "get_Player", Receiver: var pRecR } && IsOwner(pRecR)))))
                {
                    hasOwnerCheck = true;
                    continue;
                }
                // CardType check: card.Type == X
                if (IsEquality(new Test(condExpr, condExpected), true, out var tL, out var tR)
                    && tL is Call { Method.Name: "get_Type", Receiver: Input { Name: "card" } }
                    && Numeric(tR) is { } ct && Enum.IsDefined(typeof(CardType), (int)ct))
                {
                    pathType = (CardType)(int)ct;
                    continue;
                }
                // CardKeyword check: card.Keywords.Contains(K)
                if (condExpected && condExpr is Call { Method.Name: "Contains", Receiver: Call { Method.Name: "get_Keywords", Receiver: Input { Name: "card" } }, Arguments: [Constant { Data: int rawKw }] }
                    && Enum.IsDefined(typeof(CardKeyword), rawKw))
                {
                    var kwVal = (CardKeyword)rawKw;
                    if (!pathKeywords.Contains(kwVal))
                        pathKeywords.Add(kwVal);
                    continue;
                }
                // Affliction check: card.Affliction is T
                if (condExpected && condExpr is Operation { Operator: "isinst", Left: Call { Method.Name: "get_Affliction" }, Right: Constant { Data: Type affType } }
                    && typeof(AfflictionModel).IsAssignableFrom(affType))
                {
                    pathAffliction = affType;
                    continue;
                }
                if (condExpected && condExpr is Operation { Operator: "cgt.un",
                        Left: Operation { Operator: "isinst", Left: Call { Method.Name: "get_Affliction" }, Right: Constant { Data: Type affTypePos } },
                        Right: Constant { Data: 0 or null } }
                    && typeof(AfflictionModel).IsAssignableFrom(affTypePos))
                {
                    pathAffliction = affTypePos;
                    continue;
                }
                // Nullable PileType plumbing: card.Pile != null
                if (condExpected && condExpr is Call { Method.Name: "get_Pile", Receiver: Input { Name: "card" } })
                    continue;
                // PileType equality or inequality from switch (card.Pile?.Type)
                if (condExpr is Operation { Operator: "beq" or "ceq" or "bne.un" } pileOp
                    && UnwrapPileTypeExpr(pileOp.Left) is Input { Name: "card" }
                    && Numeric(pileOp.Right) is { } rawPt && Enum.IsDefined(typeof(PileType), (int)rawPt))
                {
                    var pileVal = (PileType)(int)rawPt;
                    var isPileEq = condExpected == (pileOp.Operator != "bne.un");
                    if (isPileEq)
                    {
                        if (pathPile is not null && pathPile != pileVal)
                            return null;
                        pathPile = pileVal;
                        continue;
                    }
                    // Record inequality so we can verify it is only a switch fallthrough over another allowed pile.
                    pathExcludedPiles.Add(pileVal);
                    continue;
                }
                // Fail closed on any unrecognized condition (e.g., turn counters on VoidFormPower or EnsemblePower).
                return null;
            }

            if (!hasOwnerCheck)
                return null;

            pathKeywords.Sort();
            reachablePaths++;
            if (firstPath)
            {
                filteredType = pathType;
                requiredKeywords.AddRange(pathKeywords);
                requiredAffliction = pathAffliction;
                firstPath = false;
            }
            else if (filteredType != pathType || !requiredKeywords.SequenceEqual(pathKeywords) || requiredAffliction != pathAffliction)
            {
                return null;
            }

            excludedPilesPerPath.Add((pathPile, pathExcludedPiles));
            if (pathPile is { } ptVal && !allowedPiles.Contains(ptVal))
                allowedPiles.Add(ptVal);
        }

        if (reachablePaths == 0 || (reachablePaths > 1 && allowedPiles.Count == 0))
            return null;
        if (allowedPiles.Count > 0 && excludedPilesPerPath.Any(p => p.PositivePile is null))
            return null;
        // Fail closed on standalone `card.Pile?.Type != X` exclusions not covered by a positive pile case.
        foreach (var (posPile, exclPiles) in excludedPilesPerPath)
        {
            foreach (var excl in exclPiles)
            {
                if (posPile is null || !allowedPiles.Contains(excl))
                    return null;
            }
        }

        allowedPiles.Sort();

        var subjectParts = new List<string> { "owned" };
        foreach (var kw in requiredKeywords)
            subjectParts.Add(KeywordLabel(kw));
        if (filteredType is { } kind)
            subjectParts.Add(kind.ToString());
        subjectParts.Add("cards");
        if (requiredAffliction is not null)
            subjectParts.Add($"afflicted with {ModelLabel(requiredAffliction)}");
        if (allowedPiles.Count > 0)
            subjectParts.Add($"in {string.Join(" or ", allowedPiles.Select(p => p.ToString()))}");
        var subject = string.Join(" ", subjectParts);
        var timingTag = hook.Name == "TryModifyEnergyCostInCombatLate" ? "In-combat Energy cost (late)" : "In-combat Energy cost";
        return setsZero
            ? $"{timingTag}: {subject} cost 0 Energy (Energy X-cost cards ignore cost modifiers and still spend all remaining Energy when paying resources)"
            : $"{timingTag}: {subject} cost +stacks Energy (Energy X-cost cards ignore cost modifiers and spend all remaining Energy when paying resources)";
    }

    private static bool IsUnreachableNullablePath(Test[] conditions)
    {
        foreach (var raw in conditions)
        {
            var expr = raw.Value;
            var expected = raw.Expected;
            while (expr is Operation { Operator: "ceq", Left: var inner, Right: Constant { Data: 0 or null } })
            {
                expr = inner;
                expected = !expected;
            }
            if (expr is Call { Method.Name: "get_HasValue", Method.DeclaringType: { IsGenericType: true } nullableDecl } hvCall
                && nullableDecl.GetGenericTypeDefinition() == typeof(Nullable<>))
            {
                if ((expected && hvCall.Receiver is Constant { Data: 0 })
                    || (!expected && hvCall.Receiver is Call { Method: ConstructorInfo }))
                {
                    return true;
                }
            }
        }
        return false;
    }

    private string? ReadCardPlayCountHook(Type power, MethodInfo hook, HashSet<string> handled)
    {
        var body = Body(hook);
        if (body.Incomplete)
            return null;
        var boosted = body.Returns.Where(r => r.Value is Operation { Operator: "add", Left: Input { Name: "playCount" }, Right: Constant { Data: 1 } }).ToArray();
        if (boosted is not [var active])
            return null;

        var hasOwnerCheck = false;
        var excludesDupe = false;
        CardType? filteredType = null;
        var firstPlaysPerTurn = false;
        foreach (var rawCond in active.Conditions)
        {
            var condExpr = rawCond.Value;
            var condExpected = rawCond.Expected;
            while (condExpr is Operation { Operator: "ceq", Left: var inner, Right: Constant { Data: 0 or null } })
            {
                condExpr = inner;
                condExpected = !condExpected;
            }
            var cond = new Test(condExpr, condExpected);
            if (IsCardOwnerCheck(cond, "card"))
            {
                hasOwnerCheck = true;
                continue;
            }
            if (!condExpected && condExpr is Call { Method.Name: "get_IsDupe", Receiver: Input { Name: "card" } })
            {
                excludesDupe = true;
                continue;
            }
            if (IsEquality(cond, true, out var l, out var r) && l is Call { Method.Name: "get_Type", Receiver: Input { Name: "card" } }
                && Numeric(r) is { } ct && Enum.IsDefined(typeof(CardType), (int)ct))
            {
                var nextType = (CardType)(int)ct;
                if (filteredType is not null && filteredType != nextType)
                    return null;
                filteredType = nextType;
                continue;
            }
            if (IsCountLessThanStacksCheck(cond, out var countPred) && IsOwnerCardPlaysStartedPredicate(countPred, out var countedTypes) && countedTypes.Count == 0)
            {
                firstPlaysPerTurn = true;
                continue;
            }
            // Fail closed on any unrecognized condition (e.g., Relic.UsedThisCombat on ThrowingAxe).
            return null;
        }
        if (!hasOwnerCheck)
            return null;

        var consumeSuffix = "";
        if (FindOverride(power, "AfterModifyingCardPlayCount") is { } afterHook)
        {
            if (IsSelfDecrement(afterHook))
            {
                handled.Add(afterHook.Name);
                consumeSuffix = $", consuming 1 {PowerLabel(power)} stack when play starts (before card effects/nested plays resolve)";
            }
            else if (IsPurePresentationHook(afterHook))
            {
                handled.Add(afterHook.Name);
            }
            else
            {
                return null;
            }
        }

        var dupeQualifier = excludesDupe ? "non-dupe " : "";
        var subject = firstPlaysPerTurn
            ? filteredType is { } firstKind
                ? $"owned {dupeQualifier}{firstKind} cards played among owner's first N card plays each turn (where N = stacks)"
                : $"each of owner's first N {dupeQualifier}card plays each turn (where N = stacks)"
            : filteredType is { } kind
                ? $"owned {dupeQualifier}{kind} cards"
                : $"owned {dupeQualifier}cards";
        return $"Card replay: plays {subject} 1 additional time{consumeSuffix}";
    }

    private string? ReadCardPlayResultLocationHook(Type power, MethodInfo hook, HashSet<string> handled)
    {
        var body = Body(hook);
        if (body.Incomplete)
            return null;

        int? rawPile = null;
        Test[]? conditions = null;
        var redirected = body.Returns.Where(r => r.Value is Constant { Data: int p } && Enum.IsDefined(typeof(PileType), p)).ToArray();
        if (redirected is [var activeRet] && activeRet.Value is Constant { Data: int retPile })
        {
            rawPile = retPile;
            conditions = activeRet.Conditions;
        }
        else
        {
            var locSteps = body.FieldSteps.Where(f => f.Assignment.Target.Receiver is Input { Name: "location" }
                && f.Assignment.Target.Member.Name == "pileType"
                && f.Assignment.Assigned is Constant { Data: int fp } && Enum.IsDefined(typeof(PileType), fp)).ToArray();
            if (locSteps is [var locStep] && locStep.Assignment.Assigned is Constant { Data: int fieldPile })
            {
                rawPile = fieldPile;
                conditions = locStep.Conditions;
            }
        }
        if (rawPile is null || conditions is null)
            return null;
        // Fail closed when the destination depends on internal turn counters or resource costs (handled by specialized power readers).
        if (conditions.Any(c => ContainsInternalDataOrEnergyCheck(c.Value)))
            return null;

        var hasOwnerCheck = false;
        var onlyFromDiscard = false;
        var filteredTypes = new List<CardType>();
        var firstPlaysPerTurn = false;
        foreach (var rawCond in conditions)
        {
            var condExpr = rawCond.Value;
            var condExpected = rawCond.Expected;
            while (condExpr is Operation { Operator: "ceq", Left: var inner, Right: Constant { Data: 0 or null } })
            {
                condExpr = inner;
                condExpected = !condExpected;
            }
            var cond = new Test(condExpr, condExpected);
            if (IsCardOwnerCheck(cond, "card"))
            {
                hasOwnerCheck = true;
                continue;
            }
            if (IsEquality(cond, true, out var pileL, out var pileR)
                && pileL is Field { Member.Name: "pileType", Receiver: Input { Name: "location" } }
                && Numeric(pileR) is { } reqPile && reqPile == (int)PileType.Discard)
            {
                onlyFromDiscard = true;
                continue;
            }
            if (IsEquality(cond, true, out var l, out var r) && l is Call { Method.Name: "get_Type", Receiver: Input { Name: "card" } }
                && Numeric(r) is { } ct && Enum.IsDefined(typeof(CardType), (int)ct))
            {
                var cardType = (CardType)(int)ct;
                if (!filteredTypes.Contains(cardType))
                    filteredTypes.Add(cardType);
                continue;
            }
            if (TryMatchCardTypeRange(cond, out var rangeTypes))
            {
                foreach (var rt in rangeTypes)
                    if (!filteredTypes.Contains(rt))
                        filteredTypes.Add(rt);
                continue;
            }
            if (IsCountLessThanStacksCheck(cond, out var countPred)
                && IsOwnerCardPlaysStartedPredicate(countPred, out var countedTypes)
                && (countedTypes.Count == 0 || countedTypes.Order().SequenceEqual(filteredTypes.Order())))
            {
                firstPlaysPerTurn = true;
                continue;
            }
            return null;
        }
        if (!hasOwnerCheck)
            return null;

        var posStep = body.FieldSteps.LastOrDefault(f =>
            f.Assignment.Target.Receiver is Input { Name: "location" }
            && f.Assignment.Target.Member.Name == "position"
            && ConditionsImply(conditions, f.Conditions));
        var posSuffix = "";
        if (posStep is not null)
        {
            if (posStep.Assignment.Assigned is not Constant { Data: int rawPos } || !Enum.IsDefined(typeof(CardPilePosition), rawPos))
                return null;
            posSuffix = $" ({((CardPilePosition)rawPos).ToString().ToLowerInvariant()})";
        }

        var consumeSuffix = "";
        if (FindOverride(power, "AfterModifyingCardPlayResultLocation") is { } afterLoc)
        {
            if (IsPurePresentationHook(afterLoc))
            {
                handled.Add(afterLoc.Name);
            }
            else if (IsSelfDecrement(afterLoc))
            {
                handled.Add(afterLoc.Name);
                consumeSuffix = $", consuming 1 {PowerLabel(power)} stack when play starts (before card effects/nested plays resolve)";
            }
            else
            {
                return null;
            }
        }

        var typeLabel = filteredTypes.Count > 0 ? $"{string.Join(" or ", filteredTypes)} " : "";
        var subject = firstPlaysPerTurn
            ? $"owner's first N {typeLabel}plays each turn (where N = stacks)"
            : $"owned {typeLabel}cards";
        var destLabel = PileLocationLabel((PileType)rawPile.Value);
        var insteadSuffix = onlyFromDiscard ? " instead of discard pile" : "";
        return $"Card destination after play: {subject} move to {destLabel}{posSuffix}{insteadSuffix}{consumeSuffix}";
    }

    private static bool IsCardOwnerCheck(Test cond, string cardParamName)
        => IsEquality(cond, true, out var eqL, out var eqR)
            && ((eqL is Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_Owner", Receiver: Input { Name: var n1 } } } && n1 == cardParamName && IsOwner(eqR))
                || (eqR is Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_Owner", Receiver: Input { Name: var n2 } } } && n2 == cardParamName && IsOwner(eqL))
                || (eqL is Call { Method.Name: "get_Owner", Receiver: Input { Name: var n3 } } && n3 == cardParamName
                    && (IsOwner(eqR) || eqR is Call { Method.Name: "get_Player", Receiver: var pRecL } && IsOwner(pRecL)))
                || (eqR is Call { Method.Name: "get_Owner", Receiver: Input { Name: var n4 } } && n4 == cardParamName
                    && (IsOwner(eqL) || eqL is Call { Method.Name: "get_Player", Receiver: var pRecR } && IsOwner(pRecR))));

    private static bool TryMatchCardTypeRange(Test cond, out List<CardType> matchedTypes)
    {
        matchedTypes = [];
        if (cond.Value is not Operation { Operator: "bgt.un" or "cgt.un" or "ble.un" or "cle.un",
                Left: Operation { Operator: "sub", Left: Call { Method.Name: "get_Type" }, Right: Constant { Data: int offset } },
                Right: Constant { Data: int span } } op)
            return false;
        var inRange = op.Operator is "ble.un" or "cle.un" ? cond.Expected : !cond.Expected;
        if (!inRange || span < 0 || span > 4)
            return false;
        for (var v = offset; v <= offset + span; v++)
        {
            if (!Enum.IsDefined(typeof(CardType), v))
                return false;
            matchedTypes.Add((CardType)v);
        }
        return matchedTypes.Count > 0;
    }

    private static bool IsCountLessThanStacksCheck(Test cond, out MethodInfo? predicate)
    {
        predicate = null;
        if (cond.Value is not Operation { Operator: "bge" or "blt" or "clt",
                Left: Call { Method.Name: nameof(Enumerable.Count),
                    Arguments: [Call { Method.Name: "get_CardPlaysStarted" }, Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo pred }] }] },
                Right: var rhs } op)
            return false;
        if (!IsStackAmount(rhs))
            return false;
        var isLess = op.Operator is "blt" or "clt" ? cond.Expected : !cond.Expected;
        if (!isLess)
            return false;
        predicate = pred;
        return true;
    }

    private bool IsOwnerCardPlaysStartedPredicate(MethodInfo? pred, out List<CardType> filteredTypes)
    {
        filteredTypes = [];
        if (pred is null || pred.ContainsGenericParameters)
            return false;
        var body = Body(pred);
        if (body.Incomplete || body.Returns.Count == 0)
            return false;
        var positive = body.Returns.Where(r => !(TryTruth(r.Value, out var b) && !b)).ToArray();
        if (positive.Length != 1)
            return false;
        var tests = new List<Test>(positive[0].Conditions);
        if (!(TryTruth(positive[0].Value, out var retBool) && retBool))
            tests.Add(new Test(positive[0].Value, true));

        var hasTurnCheck = false;
        var hasOwnerCheck = false;
        foreach (var raw in tests)
        {
            var expr = raw.Value;
            var expected = raw.Expected;
            while (expr is Operation { Operator: "ceq", Left: var inner, Right: Constant { Data: 0 or null } })
            {
                expr = inner;
                expected = !expected;
            }
            if (expected && expr is Call { Method.Name: "HappenedThisTurn" })
            {
                hasTurnCheck = true;
                continue;
            }
            if (expected && expr is Call { Method.Name: "get_IsFirstInSeries" })
                continue;
            if (IsEquality(new Test(expr, expected), true, out var eqL, out var eqR)
                && ((IsEntryOwnerExpr(eqL) && IsOwnerPlayerOrCreature(eqR))
                    || (IsEntryOwnerExpr(eqR) && IsOwnerPlayerOrCreature(eqL))))
            {
                hasOwnerCheck = true;
                continue;
            }
            if (IsEquality(new Test(expr, expected), true, out var tl, out var tr)
                && tl is Call { Method.Name: "get_Type" } && Numeric(tr) is { } ct && Enum.IsDefined(typeof(CardType), (int)ct))
            {
                var cardType = (CardType)(int)ct;
                if (!filteredTypes.Contains(cardType))
                    filteredTypes.Add(cardType);
                continue;
            }
            if (TryMatchCardTypeRange(new Test(expr, expected), out var rangeTypes))
            {
                foreach (var rt in rangeTypes)
                    if (!filteredTypes.Contains(rt))
                        filteredTypes.Add(rt);
                continue;
            }
            return false;
        }
        return hasTurnCheck && hasOwnerCheck;
    }

    private static bool IsEntryOwnerExpr(Value? value)
        => value is Call { Method.Name: "get_Actor" }
            || value is Call { Method.Name: "get_Player", Receiver: Call { Method.Name: "get_CardPlay" } };

    private static bool IsOwnerPlayerOrCreature(Value? value)
        => IsOwnerOrClosureOwner(value)
            || value is Call { Method.Name: "get_Player", Receiver: var rec } && IsOwnerOrClosureOwner(rec);

    private bool IsPurePresentationHook(MethodInfo method)
        => Body(method) is { Incomplete: false, Writes.Count: 0, FieldSteps.Count: 0 } body
            && body.Calls.All(step => IsBenignReadOrPlumbingCall(step.Call.Method));

    private static bool HasInternalDataFieldAccess(MethodInfo hook)
        => NativeRuleBody.Read(hook) is { Incomplete: false } body
            && body.Returns.SelectMany(r => r.Conditions).Any(c => ContainsInternalDataOrEnergyCheck(c.Value));

    private static bool ContainsInternalDataOrEnergyCheck(Value value) => value switch
    {
        Field f => InternalDataField(f) is not null,
        Call { Method.Name: "get_EnergyValue" or "get_EnergySpent" } => true,
        Call c => (c.Receiver is { } rec && ContainsInternalDataOrEnergyCheck(rec))
            || c.Arguments.Any(ContainsInternalDataOrEnergyCheck),
        Operation op => ContainsInternalDataOrEnergyCheck(op.Left)
            || (op.Right is { } right && ContainsInternalDataOrEnergyCheck(right)),
        _ => false,
    };

    private string? ReadOrbPassiveTriggerCountHook(Type type, MethodInfo hook, HashSet<string> handled)
    {
        var body = Body(hook);
        if (body.Incomplete || body.Returns.Count == 0)
            return null;
        var outcomes = new List<(string Text, Test[] Conditions)>();
        foreach (var result in body.Returns)
        {
            if (result.Value is Input { Name: "triggerCount" })
            {
                outcomes.Add(("", result.Conditions));
                continue;
            }
            if (result.Value is Operation { Operator: "add", Left: Input { Name: "triggerCount" }, Right: var delta }
                && ValueText(delta) is { } deltaText)
            {
                outcomes.Add(($"+{deltaText} additional passive trigger(s)", result.Conditions));
                continue;
            }
            return null;
        }
        var rendered = RenderOutcomes(outcomes, body.Terminals.Where(t => !t.Returned).Select(t => t.Conditions));
        if (rendered is not { Count: > 0 })
            return null;
        if (FindOverride(type, "AfterModifyingOrbPassiveTriggerCount") is { } afterHook && IsPurePresentationHook(afterHook))
            handled.Add(afterHook.Name);
        return $"Orb passive trigger count: {string.Join("; ", rendered)}";
    }

    private void ReadPowerReceptionHooks(Type type, string powerPrefix, HashSet<string> handled, List<string> lines)
    {
        if (FindOverride(type, "TryModifyPowerAmountReceived") is { } modify)
        {
            handled.Add(modify.Name);
            if (IsVisibleDebuffNegation(modify))
            {
                var decrement = FindOverride(type, "AfterModifyingPowerAmountReceived") is { } after && IsSelfDecrement(after);
                if (decrement)
                    handled.Add("AfterModifyingPowerAmountReceived");
                var outcome = decrement
                    ? $"(reduce applied stacks to 0; attempt to subtract 1 {PowerLabel(type)} stack)"
                    : "reduce applied stacks to 0";
                lines.Add($"{powerPrefix}When a power would be applied to owner: if applied power is a visible Debuff: {outcome}");
            }
            else if (ReadPowerAmountReceivedModifier(type, modify, handled) is { } receivedLine)
            {
                lines.Add($"{powerPrefix}{receivedLine}");
            }
            else
            {
                Gap($"{type.Name}.{modify.Name}");
            }
        }
        if (FindOverride(type, "ModifyPowerAmountGivenAdditive") is { } givenAdd)
        {
            handled.Add(givenAdd.Name);
            var rendered = ReadValueHook(givenAdd, 0m, "+{0}", out _);
            if (rendered is { Count: > 0 })
            {
                if (FindOverride(type, "AfterModifyingPowerAmountGiven") is { } afterGiven && IsPurePresentationHook(afterGiven))
                    handled.Add(afterGiven.Name);
                lines.Add($"{powerPrefix}Power stacks given (additive): {string.Join("; ", rendered)}");
            }
            else
            {
                Gap($"{type.Name}.{givenAdd.Name}");
            }
        }
    }

    private string? ReadPowerAmountReceivedModifier(Type type, MethodInfo method, HashSet<string> handled)
    {
        var body = Body(method);
        if (body.Incomplete)
            return null;
        var active = body.Returns.Where(r => TryTruth(r.Value, out var modified) && modified).ToArray();
        var nonPassthroughStores = body.OutStores.Where(s => s.Parameter.Name == "modifiedAmount" && s.Assigned is not Input).ToArray();
        if (active.Length != 1 || nonPassthroughStores is not [var store])
            return null;

        string? changeText = null;
        if (store.Assigned is Call { Method.Name: "op_Multiply", Arguments: [var mulL, var mulR] }
            && ((mulL is Input { Name: "amount" } && ValueText(mulR) is { } mulFactor)
                || (mulR is Input { Name: "amount" } && ValueText(mulL) is { } mulFactor2 && (mulFactor = mulFactor2) is not null)))
        {
            changeText = $"multiply applied stacks by {mulFactor}";
        }
        else if (store.Assigned is Call { Method.Name: "op_Addition", Arguments: [Input { Name: "amount" }, var addR] }
            && ValueText(addR) is { } addText)
        {
            changeText = $"increase applied stacks by {addText}";
        }
        if (changeText is null)
            return null;

        var guard = Guard(active[0].Conditions);
        if (guard is null || guard == Never)
            return null;

        var afterSuffix = "";
        if (FindOverride(type, "AfterModifyingPowerAmountReceived") is { } afterHook)
        {
            var (afterEffects, afterIncomplete) = ReadEffects(afterHook, typeof(PowerModel).IsAssignableFrom(type) ? type : null);
            if (!afterIncomplete)
            {
                handled.Add(afterHook.Name);
                if (afterEffects.Count > 0)
                    afterSuffix = $"; then {string.Join("; ", afterEffects.Select(e => e.Text))}";
            }
        }
        return $"When a power would be applied: {When(guard, changeText + afterSuffix)}";
    }

    private bool IsVisibleDebuffNegation(MethodInfo method)
    {
        if (Body(method) is not { Incomplete: false } body)
            return false;
        var blocked = body.Returns.Where(result => TryTruth(result.Value, out var modified) && modified).ToArray();
        var passed = body.Returns.Where(result => TryTruth(result.Value, out var modified) && !modified).ToArray();
        if (blocked.Length != 1 || passed.Length == 0)
            return false;
        var writesZero = body.OutStores.Any(store => store.Parameter.Name == "modifiedAmount" && Numeric(store.Assigned) == 0m);
        if (!writesZero)
            return false;
        var conditions = blocked[0].Conditions;
        var checksOwner = conditions.Any(test => IsEquality(test, true, out var left, out var right)
            && left is Input { Name: "target" } && IsOwner(right));
        var checksDebuff = conditions.Any(test => IsEquality(test, true, out var left, out var right)
            && left is Call { Method.Name: "GetTypeForAmount", Receiver: Input { Name: "canonicalPower" } }
            && Numeric(right) == (int)PowerType.Debuff);
        var checksVisible = conditions.Any(test => test is { Expected: true, Value: Call
            { Method.Name: "get_IsVisible", Receiver: Input { Name: "canonicalPower" } } });
        return checksOwner && checksDebuff && checksVisible;
    }

    private bool IsSelfDecrement(MethodInfo method)
        => Body(method) is { Incomplete: false, Writes.Count: 0, FieldSteps.Count: 0 } body
            && body.Calls.All(step => IsBenignReadOrPlumbingCall(step.Call.Method)
                || (step.Call.Method.DeclaringType == typeof(PowerCmd) && step.Call.Method.Name == nameof(PowerCmd.Decrement)))
            && body.Calls.Where(step => step.Call.Method.DeclaringType == typeof(PowerCmd)).DistinctBy(step => step.Index).ToArray()
                is [{ Conditions: var decConds, Call: { Method.Name: nameof(PowerCmd.Decrement), Arguments: [Input { Name: "this" }] } }]
            && decConds.All(c => IsCardOwnerCheck(c, "card"));

    private void ReadDamageCapHooks(Type type, string powerPrefix, HashSet<string> handled, List<string> lines)
    {
        if (FindOverride(type, "ModifyDamageCap") is { } capHook)
        {
            handled.Add(capHook.Name);
            var rendered = ReadDamageCap(capHook);
            if (rendered is null)
                Gap($"{type.Name}.{capHook.Name}");
            else if (rendered.Count > 0)
                lines.Add($"{powerPrefix}Damage cap per hit: {string.Join("; ", rendered)}");
        }
        if (FindOverride(type, "ModifyHpLostAfterOsty") is { } hpCapHook)
        {
            handled.Add(hpCapHook.Name);
            var rendered = ReadHpLossCap(hpCapHook);
            if (rendered is null)
                Gap($"{type.Name}.{hpCapHook.Name}");
            else if (rendered.Count > 0)
                lines.Add($"{powerPrefix}HP loss cap per hit, after block: {string.Join("; ", rendered)}");
        }
        if (HasSideTurnHpLossCap(type))
        {
            handled.Add("ModifyHpLostBeforeOstyLate");
            handled.Add("AfterDamageReceived");
            handled.Add("BeforeSideTurnStart");
            lines.Add($"{powerPrefix}HP loss cap per side turn: total unblocked HP lost by owner during each side turn is capped at stacks (resets before every side turn starts)");
        }
        if (FindOverride(type, "ModifyHpLostAfterOstyLate") is { } bufferHook)
        {
            handled.Add(bufferHook.Name);
            if (ReadZeroHpLossLateHook(type, bufferHook, handled) is { } bufferLine)
                lines.Add($"{powerPrefix}{bufferLine}");
            else
                Gap($"{type.Name}.{bufferHook.Name}");
        }
    }

    private string? ReadZeroHpLossLateHook(Type power, MethodInfo hook, HashSet<string> handled)
    {
        var body = Body(hook);
        if (body.Incomplete)
            return null;
        var zeroed = body.Returns.Where(r => Numeric(r.Value) == 0m).ToArray();
        if (zeroed is not [var active])
            return null;

        var hasOwnerCheck = false;
        var excludedCards = new List<Type>();
        foreach (var rawCond in active.Conditions)
        {
            var expr = rawCond.Value;
            var expected = rawCond.Expected;
            while (expr is Operation { Operator: "ceq", Left: var inner, Right: Constant { Data: 0 or null } })
            {
                expr = inner;
                expected = !expected;
            }
            if (IsEquality(new Test(expr, expected), true, out var eqL, out var eqR)
                && ((eqL is Input { Name: "target" } && IsOwner(eqR))
                    || (eqR is Input { Name: "target" } && IsOwner(eqL))))
            {
                hasOwnerCheck = true;
                continue;
            }
            if (!expected && expr is Operation { Operator: "isinst", Left: Input { Name: "cardSource" }, Right: Constant { Data: Type cardType } }
                && typeof(CardModel).IsAssignableFrom(cardType))
            {
                if (!excludedCards.Contains(cardType))
                    excludedCards.Add(cardType);
                continue;
            }
            if (!expected && expr is Operation { Operator: "cgt.un",
                    Left: Operation { Operator: "isinst", Left: Input { Name: "cardSource" }, Right: Constant { Data: Type cardTypePos } },
                    Right: Constant { Data: 0 or null } }
                && typeof(CardModel).IsAssignableFrom(cardTypePos))
            {
                if (!excludedCards.Contains(cardTypePos))
                    excludedCards.Add(cardTypePos);
                continue;
            }
            return null;
        }
        if (!hasOwnerCheck)
            return null;

        var consumeSuffix = "";
        if (FindOverride(power, "AfterModifyingHpLostAfterOsty") is { } afterHook)
        {
            if (IsSelfDecrement(afterHook))
            {
                handled.Add(afterHook.Name);
                consumeSuffix = $" and subtract 1 {PowerLabel(power)} stack";
            }
            else if (IsPurePresentationHook(afterHook))
            {
                handled.Add(afterHook.Name);
            }
            else
            {
                return null;
            }
        }
        var exceptSuffix = excludedCards.Count > 0
            ? $" (except from {string.Join(" or ", excludedCards.Select(ModelLabel))})"
            : "";
        return $"Prevent next HP loss: when owner would lose nonzero HP{exceptSuffix} (checked after Block and {ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty))} redirection), reduce HP loss to 0{consumeSuffix}";
    }

    private List<string>? ReadDamageCap(MethodInfo hook)
    {
        var body = Body(hook);
        if (body.Incomplete)
            return null;
        var outcomes = new List<(string Text, Test[] Conditions)>();
        foreach (var result in body.Returns)
        {
            if (Numeric(result.Value) == decimal.MaxValue)
                outcomes.Add(("", result.Conditions));
            else if (ValueText(result.Value) is { } cap)
                outcomes.Add(($"at most {cap}", result.Conditions));
            else
                return null;
        }
        return RenderOutcomes(outcomes, body.Terminals.Where(terminal => !terminal.Returned).Select(terminal => terminal.Conditions));
    }

    private List<string>? ReadHpLossCap(MethodInfo hook)
    {
        var body = Body(hook);
        if (body.Incomplete)
            return null;
        var capped = body.Returns.Where(result => result.Value is not Input { Name: "amount" }).ToArray();
        if (capped is not [var active] || ValueText(active.Value) is not { } bound)
            return null;
        var ownerOnly = active.Conditions.Where(test =>
            !(test.Value is Call { Method.Name: "op_LessThan", Method.DeclaringType: var d, Arguments: [Input { Name: "amount" }, var limit] }
              && d == typeof(decimal) && ValueText(limit) == bound)).ToArray();
        return Guard(ownerOnly) is { } guard && guard != Never
            ? [When(guard, $"at most {bound}")]
            : null;
    }

    private bool HasSideTurnHpLossCap(Type power)
    {
        if (FindOverride(power, "ModifyHpLostBeforeOstyLate") is not { } cap
            || FindOverride(power, "AfterDamageReceived") is not { } track
            || FindOverride(power, "BeforeSideTurnStart") is not { } reset
            || Body(cap) is not { Incomplete: false } capBody
            || Body(track) is not { Incomplete: false } trackBody
            || Body(reset) is not { Incomplete: false, FieldSteps: [var clear] })
            return false;
        if (clear.Conditions.Length != 0 || InternalDataField(clear.Assignment.Target) is not { } counter
            || Numeric(clear.Assignment.Assigned) != 0m)
            return false;
        var usesRemaining = capBody.Returns.Any(result => result.Value is Call
            { Method.Name: nameof(Math.Min), Arguments: [Input { Name: "amount" },
                Call { Method.Name: "op_Subtraction", Arguments: [var max, Field recorded] }] }
            && IsStackAmount(max) && InternalDataField(recorded) == counter);
        var addsUnblocked = trackBody.FieldSteps.Any(step => InternalDataField(step.Assignment.Target) == counter
            && step.Assignment.Assigned is Call { Method.Name: "op_Addition" });
        return usesRemaining && addsUnblocked;
    }

    private static FieldInfo? InternalDataField(Field field)
        => field.Receiver is Call { Method: { Name: "GetInternalData", IsGenericMethod: true } getter,
            Receiver: Input { Name: "this" }, Arguments.Length: 0 }
            && getter.DeclaringType == typeof(PowerModel)
            && getter.GetGenericArguments()[0] == field.Member.DeclaringType
                ? field.Member
                : null;

    private string? ReadPlayerDeltaHook(Type power, MethodInfo hook, string operand, string label)
    {
        var body = Body(hook);
        if (body.Incomplete || body.Returns.Count == 0)
            return null;

        var outcomes = new List<(string Text, Test[] Conditions)>();
        foreach (var result in body.Returns)
        {
            var value = result.Value;
            if (value is Input pass && pass.Name == operand)
            {
                outcomes.Add(("", result.Conditions));
                continue;
            }
            var floored = false;
            if (value is Call { Method.DeclaringType: var math, Method.Name: nameof(Math.Max), Arguments: [var arg0, var arg1] }
                && math == typeof(Math))
            {
                if (Numeric(arg0) == 0m)
                {
                    floored = true;
                    value = arg1;
                }
                else if (Numeric(arg1) == 0m)
                {
                    floored = true;
                    value = arg0;
                }
            }
            if (value is Call { Method.DeclaringType: var number, Method.Name: "op_Addition" or "op_Subtraction", Arguments: [Input source, var delta] }
                && number == typeof(decimal) && source.Name == operand && ValueText(delta) is { } deltaText)
            {
                var verb = value is Call { Method.Name: "op_Addition" } ? "increase by" : "reduce by";
                var suffix = floored ? " (minimum 0)" : "";
                outcomes.Add(($"{verb} {deltaText}{suffix}", result.Conditions));
                continue;
            }
            return null;
        }
        var rendered = RenderOutcomes(outcomes, body.Terminals.Where(t => !t.Returned).Select(t => t.Conditions));
        return rendered is { Count: > 0 } ? $"{label}: {string.Join("; ", rendered)}" : null;
    }

    private bool HasCardPlayLimit(Type power)
    {
        if (FindOverride(power, "ShouldPlay") is not { } check || Body(check) is not { Incomplete: false, Returns.Count: 2 } query
            || query.Returns.SingleOrDefault(result => result.Conditions is [var test] && IsCardOwner(test, true))?.Value
                is not Operation { Operator: "clt", Left: Field { Receiver: Input { Name: "this" } } counter, Right: var limit }
            || !IsStackAmount(limit)
            || query.Returns.SingleOrDefault(result => result.Conditions is [var test] && IsCardOwner(test, false))?.Value is not Constant { Data: 1 }
            || FindOverride(power, "BeforeCardPlayed") is not { } increment
            || Body(increment) is not { Incomplete: false, FieldSteps: [var add] }
            || add.Assignment.Target != counter || add.Assignment.Assigned is not Operation { Operator: "add", Left: Field previous, Right: Constant { Data: 1 } }
            || previous != counter || add.Conditions is not [var playing] || !IsHolderPlayer(playing, "cardPlay", true)
            || FindOverride(power, "BeforeSideTurnStart") is not { } reset
            || Body(reset) is not { Incomplete: false, FieldSteps: [var clear] }
            || clear.Assignment.Target != counter || clear.Assignment.Assigned is not Constant { Data: 0 }
            || clear.Conditions is not [var participating] || !IsHolderParticipating(participating))
            return false;
        return true;
    }

    private static bool IsCardOwner(Test test, bool expected)
        => IsEquality(test, expected, out var left, out var right)
            && left is Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_Owner",
                Receiver: Input { Name: "card" } } }
            && IsOwner(right);

    private static bool IsHolderPlayer(Test test, string operand, bool expected)
        => IsEquality(test, expected, out var left, out var right)
            && right is Call { Method.DeclaringType: var creature, Method.Name: "get_Player", Receiver: var holder }
            && creature == typeof(Creature) && IsOwner(holder)
            && (left is Input input && input.Name == operand
                || left is Call { Method.DeclaringType: var model, Method.Name: "get_Owner",
                    Receiver: Call { Method.DeclaringType: var play, Method.Name: "get_Card", Receiver: Input cardPlay } }
                && model == typeof(CardModel) && play == typeof(CardPlay) && cardPlay.Name == operand);

    private static bool IsEquality(Test test, bool expected, out Value? left, out Value? right)
    {
        if (test.Value is Operation { Operator: "beq" or "ceq" or "bne.un" } comparison
            && test.Expected == (expected == (comparison.Operator != "bne.un")))
        {
            left = comparison.Left;
            right = comparison.Right;
            return true;
        }
        left = right = null;
        return false;
    }

    private static bool IsHolderParticipating(Test test)
        => test is { Expected: true, Value: Call { Method.DeclaringType: var enumerable, Method.Name: nameof(Enumerable.Contains),
            Arguments: [Input { Name: "participants" }, var holder] } } && enumerable == typeof(Enumerable) && IsOwner(holder);

    private static bool IsStackAmount(Value? amount)
    {
        if (amount is Call { Method.DeclaringType: var convert, Method.Name: "op_Implicit", Arguments: [var inner] } && convert == typeof(decimal))
            amount = inner;
        return amount is Call { Method.DeclaringType: var power, Method.Name: "get_Amount", Receiver: Input { Name: "this" } }
            && power == typeof(PowerModel);
    }
}
