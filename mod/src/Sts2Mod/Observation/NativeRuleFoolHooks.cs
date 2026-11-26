using System.Reflection;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Commands.Builders;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.FoolArrows;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Localization.DynamicVars;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Cards;
using MegaCrit.Sts2.Core.Models.Powers;
using MegaCrit.Sts2.Core.ValueProps;
using static Sts2Mod.Observation.NativeRuleBody;

namespace Sts2Mod.Observation;

internal sealed partial class NativeRuleText
{
    private void ReadFoolHooks(Type type, string prefix, HashSet<string> handled, List<string> lines)
    {
        if (typeof(PowerModel).IsAssignableFrom(type))
        {
            if (ReadStretchHooks(type, prefix, out var stretchLine))
            {
                handled.Add("BeforeCardPlayed");
                handled.Add("ModifyBlockAdditive");
                handled.Add("AfterCardPlayed");
                lines.Add(stretchLine);
            }
            if (ReadQuietusHooks(type, prefix, out var quietusLine))
            {
                handled.Add("BeforeAttack");
                handled.Add("ModifyDamageMultiplicative");
                handled.Add("AfterAttack");
                lines.Add(quietusLine);
            }
            if (ReadEncoreHooks(type, prefix, out var encoreLine))
            {
                handled.Add("AfterApplied");
                handled.Add("BeforeCardPlayed");
                handled.Add("ShouldPlay");
                handled.Add("AfterCardPlayed");
                lines.Add(encoreLine);
            }
            if (ReadImpaledHooks(type, prefix, out var impaledLines))
            {
                handled.Add("AfterApplied");
                handled.Add("AfterDamageReceived");
                handled.Add("AfterDeath");
                lines.AddRange(impaledLines);
            }
            if (ReadSimpletonHook(type, prefix, out var simpletonLine))
            {
                handled.Add("AfterAutoPostPlayPhaseEntered");
                lines.Add(simpletonLine);
            }
            if (ReadMelodyFirstPlayCountHook(type, prefix, out var melodyCountLine))
            {
                handled.Add("AfterCardPlayed");
                lines.Add(melodyCountLine);
            }
            if (ReadEnsembleHooks(type, prefix, out var ensembleLines))
            {
                handled.Add("TryModifyEnergyCostInCombatLate");
                handled.Add("AfterCardPlayed");
                lines.AddRange(ensembleLines);
            }
            if (ReadTeammateDeathEnergyHook(type, prefix, out var compostLine))
            {
                handled.Add("AfterDeath");
                lines.Add(compostLine);
            }
            if (ReadCoveringFireHook(type, prefix, out var coveringFireLine))
            {
                handled.Add("AfterArrowShot");
                lines.Add(coveringFireLine);
            }
            if (ReadEcstasyHook(type, prefix, out var ecstasyLine))
            {
                handled.Add("AfterPlayerTurnStart");
                lines.Add(ecstasyLine);
            }
            if (ReadEnnuiBeforeHandDrawHook(type, prefix, out var ennuiLine))
            {
                handled.Add("BeforeHandDraw");
                lines.Add(ennuiLine);
            }
            if (ReadMythicFormHook(type, prefix, out var mythicLine))
            {
                handled.Add("BeforeHandDraw");
                lines.Add(mythicLine);
            }
            if (ReadShowOffDeathHook(type, prefix, out var showOffLine))
            {
                handled.Add("AfterDeath");
                lines.Add(showOffLine);
            }
            if (ReadTuneHook(type, prefix, out var tuneLine))
            {
                handled.Add("AfterCardPlayed");
                lines.Add(tuneLine);
            }
        }
        else if (typeof(RelicModel).IsAssignableFrom(type))
        {
            if (ReadBizarreLampHook(type, prefix, out var bizarreLampLine))
            {
                handled.Add("BeforeHandDraw");
                lines.Add(bizarreLampLine);
            }
            if (ReadOcarinaHook(type, prefix, out var ocarinaLine))
            {
                handled.Add("BeforeHandDraw");
                lines.Add(ocarinaLine);
            }
        }
    }

    private string? FoolCombatCondition(Test test)
    {
        var normalized = NormalizeCondition(test);
        var value = normalized.Value;
        var expected = normalized.Expected;

        if (value is Call { Method.DeclaringType: var decl, Method.Name: var name, Arguments: var args } && decl == typeof(MelodyCmd))
        {
            if (name == nameof(MelodyCmd.IsFirstPlayThisTurn) && args.Length == 2
                && Numeric(args[1]) is { } rawTag && Enum.IsDefined(typeof(CardTag), (int)rawTag))
            {
                var tag = (CardTag)(int)rawTag;
                return expected
                    ? $"this is the first {tag}-tagged card play this turn"
                    : $"this is not the first {tag}-tagged card play this turn";
            }
            var melodyTag = name switch
            {
                nameof(MelodyCmd.PlayedWahThisTurn) => (CardTag?)CardTag.Wah,
                nameof(MelodyCmd.PlayedChoThisTurn) => CardTag.Cho,
                nameof(MelodyCmd.PlayedKiThisTurn) => CardTag.Ki,
                _ => null,
            };
            if (melodyTag is { } playedTag && args.Length == 1 && ValueText(args[0]) is { } who)
            {
                return expected
                    ? $"{who} has played a {playedTag}-tagged card this turn"
                    : $"{who} has not played a {playedTag}-tagged card this turn";
            }
            if (name == nameof(MelodyCmd.PlayedThisTurn) && args.Length == 2
                && ValueText(args[0]) is { } pWho
                && Numeric(args[1]) is { } rawPlayedTag && Enum.IsDefined(typeof(CardTag), (int)rawPlayedTag))
            {
                var pTag = (CardTag)(int)rawPlayedTag;
                return expected
                    ? $"{pWho} has played a {pTag}-tagged card this turn"
                    : $"{pWho} has not played a {pTag}-tagged card this turn";
            }
        }

        return null;
    }

    private string? FoolCombatValue(Value value)
    {
        if (value is Call { Method.DeclaringType: var decl, Method.Name: nameof(MelodyCmd.MelodiesThisTurn), Arguments: [var playerArg] }
            && decl == typeof(MelodyCmd) && ValueText(playerArg) is { } who)
            return $"distinct Melody tags (Wah/Cho/Ki) played by {who} this turn";

        if (value is Field { Member.DeclaringType: var fieldDecl, Member.Name: "owner" }
            && fieldDecl == typeof(ArrowSlot))
            return "arrow owner";

        if (value is Call { Method.Name: "get_Creature", Receiver: Field { Member.DeclaringType: var arrowDecl, Member.Name: "owner" } }
            && arrowDecl == typeof(ArrowSlot))
            return "arrow owner";

        if (value is Call { Method.Name: "FirstOrDefault", Arguments: [Call { Method.Name: "get_Arrows", Receiver: Input { Name: "power" } }] })
            return "first impaled arrow";

        return null;
    }

    private bool TryReadGeneratedPile(Call addCall, out PileType pile, out string overflowNote)
    {
        pile = default;
        overflowNote = "";
        if (Numeric(Argument(addCall, "newPileType")) is not { } rawPile
            || !Enum.IsDefined(typeof(PileType), (int)rawPile))
            return false;
        pile = (PileType)(int)rawPile;
        overflowNote = pile == PileType.Hand ? " (overflows to discard pile if hand is full)" : "";
        return true;
    }

    private bool TryReadOwnerPileLabel(NativeRuleBody body, out string pileLabel)
    {
        pileLabel = "";
        var getPileStep = body.Calls.FirstOrDefault(s =>
            s.Call.Method.Name == "GetPile" && s.Call.Arguments.Length >= 1
            && Numeric(s.Call.Arguments[0]) is { } rp && Enum.IsDefined(typeof(PileType), (int)rp));
        if (getPileStep is null)
            return false;
        var pile = (PileType)(int)Numeric(getPileStep.Call.Arguments[0])!;
        pileLabel = pile == PileType.Hand ? "owner's hand" : $"owner's {pile.ToString().ToLowerInvariant()} pile";
        return true;
    }

    private static bool IsNamedParamOrClosureField(Value? value, string paramName)
        => (value is Input { Name: var n } && n == paramName)
            || (value is Field { Member.Name: var fn } && fn == paramName);

    private static bool HasPlayerOwnerCheck(Test[] conditions, string playerParamName = "player")
        => conditions.Any(c =>
            IsEquality(NormalizeCondition(c), true, out var l, out var r)
            && ((IsNamedParamOrClosureField(l, playerParamName) && IsOwnerPlayerOrCreature(r))
                || (IsNamedParamOrClosureField(r, playerParamName) && IsOwnerPlayerOrCreature(l))
                || (l is Call { Method.Name: "get_Creature", Receiver: var lr } && IsNamedParamOrClosureField(lr, playerParamName) && IsOwner(r))
                || (r is Call { Method.Name: "get_Creature", Receiver: var rr } && IsNamedParamOrClosureField(rr, playerParamName) && IsOwner(l))));

    private static bool HasCardPlayOwnerCheck(Test[] conditions, bool expected = true)
        => conditions.Any(c =>
            IsEquality(NormalizeCondition(c), expected, out var l, out var r)
            && ((IsCardPlayOwnerExpr(l) && IsOwnerPlayerOrCreature(r))
                || (IsCardPlayOwnerExpr(r) && IsOwnerPlayerOrCreature(l))));

    private static bool IsCardPlayOwnerExpr(Value? value) => value switch
    {
        Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_Player", Receiver: Input { Name: "cardPlay" } } } => true,
        Call { Method.Name: "get_Player", Receiver: Input { Name: "cardPlay" } } => true,
        Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_Owner", Receiver: Call { Method.Name: "get_Card", Receiver: Input { Name: "cardPlay" } } } } => true,
        Call { Method.Name: "get_Owner", Receiver: Call { Method.Name: "get_Card", Receiver: Input { Name: "cardPlay" } } } => true,
        _ => false,
    };

    private bool HasTurnAtMostCheck(Test[] conditions, out int maxTurn)
    {
        maxTurn = 0;
        foreach (var raw in conditions)
        {
            var c = NormalizeCondition(raw);
            if (c.Value is Operation { Operator: "bgt" or "cgt" or "ble" or "cle",
                    Left: Call { Method.Name: "get_TurnNumber" },
                    Right: var rhs } op
                && Numeric(rhs) is { } limit)
            {
                var isAtMost = op.Operator is "ble" or "cle" ? c.Expected : !c.Expected;
                if (isAtMost)
                {
                    maxTurn = (int)limit;
                    return true;
                }
            }
        }
        return false;
    }

    private static bool HasEnemySideCheck(Test[] conditions, string creatureParamName = "target")
        => conditions.Any(c =>
            IsEquality(NormalizeCondition(c), false, out var l, out var r)
            && ((l is Call { Method.Name: "get_Side", Receiver: Input { Name: var ln } } && ln == creatureParamName
                    && r is Call { Method.Name: "get_Side", Receiver: var rRec } && IsOwner(rRec))
                || (r is Call { Method.Name: "get_Side", Receiver: Input { Name: var rn } } && rn == creatureParamName
                    && l is Call { Method.Name: "get_Side", Receiver: var lRec } && IsOwner(lRec))));

    private static bool HasPoweredAttackCheck(Test[] conditions)
        => conditions.Any(c => NormalizeCondition(c) is { Expected: true, Value: Call { Method.Name: "IsPoweredAttack" } });

    private static bool HasNonNullCheck(Test[] conditions, string paramName)
        => conditions.Any(raw =>
        {
            var c = NormalizeCondition(raw);
            if (c.Expected && c.Value is Input { Name: var n } && n == paramName)
                return true;
            return IsEquality(c, false, out var l, out var r)
                && ((l is Input { Name: var ln } && ln == paramName && r is Constant { Data: null })
                    || (r is Input { Name: var rn } && rn == paramName && l is Constant { Data: null }));
        });

    private static bool TryExtractDelegatePredicate(Call call, out MethodInfo? predicate)
    {
        predicate = null;
        foreach (var arg in call.Arguments)
        {
            if (arg is Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo pred }] })
            {
                predicate = pred;
                return true;
            }
        }
        return false;
    }

    private bool IsDisplayVarSyncOnly(NativeRuleBody body)
        => body.FieldSteps.Count == 0
            && body.Calls.All(step =>
            {
                var m = step.Call.Method;
                if (IsBenignReadOrPlumbingCall(m))
                    return true;
                if (m.DeclaringType == typeof(DynamicVarSet) && m.Name == "get_Item")
                    return true;
                if (m.DeclaringType == typeof(decimal) && m.Name == "op_Implicit")
                    return true;
                if (m.DeclaringType is { } dt && typeof(DynamicVar).IsAssignableFrom(dt)
                    && m.Name is "set_BaseValue" or "set_StringValue")
                    return true;
                if (m.DeclaringType?.Name == "PlatformUtil" && m.Name == "GetPlayerName")
                    return true;
                return false;
            });

    private bool IsLivingTeammatePlayersPredicate(MethodInfo selector, bool mustExcludeOwner)
    {
        var body = Body(selector);
        if (body.Incomplete || body.Returns.Count == 0)
            return false;
        var positive = body.Returns.Where(r => !(TryTruth(r.Value, out var b) && !b)).ToArray();
        if (positive.Length != 1)
            return false;
        var tests = new List<Test>(positive[0].Conditions);
        if (!(TryTruth(positive[0].Value, out var retBool) && retBool))
            tests.Add(new Test(positive[0].Value, true));

        var checksAlive = false;
        var checksPlayer = false;
        var checksNotOwner = false;
        foreach (var raw in tests)
        {
            var c = NormalizeCondition(raw);
            if (c.Expected && c.Value is Input)
                continue;
            if (c.Expected && c.Value is Call { Method.Name: "get_IsAlive" })
            {
                checksAlive = true;
                continue;
            }
            if (c.Expected && c.Value is Call { Method.Name: "get_IsPlayer" })
            {
                checksPlayer = true;
                continue;
            }
            if (IsEquality(c, false, out var neqL, out var neqR)
                && (IsOwnerPlayerOrCreature(neqL) || IsOwnerPlayerOrCreature(neqR)))
            {
                checksNotOwner = true;
                continue;
            }
            return false;
        }
        return checksAlive && checksPlayer && checksNotOwner == mustExcludeOwner;
    }

    private bool ReadStretchHooks(Type power, string prefix, out string line)
    {
        line = "";
        if (FindOverride(power, "BeforeCardPlayed") is not { } before
            || FindOverride(power, "ModifyBlockAdditive") is not { } add
            || FindOverride(power, "AfterCardPlayed") is not { } after
            || Body(before) is not { Incomplete: false } beforeBody
            || Body(add) is not { Incomplete: false } addBody
            || Body(after) is not { Incomplete: false } afterBody)
            return false;

        var recordCardPlayStep = beforeBody.FieldSteps.FirstOrDefault(step => InternalDataField(step.Assignment.Target)?.FieldType == typeof(CardPlay)
            && step.Assignment.Assigned is Input { Name: "cardPlay" });
        var recordsAmount = beforeBody.FieldSteps.Any(step => InternalDataField(step.Assignment.Target)?.FieldType == typeof(int)
            && IsStackAmount(step.Assignment.Assigned));
        var gaveBonusField = addBody.FieldSteps
            .Where(step => InternalDataField(step.Assignment.Target)?.FieldType == typeof(bool)
                && TryTruth(step.Assignment.Assigned, out var v) && v)
            .Select(step => InternalDataField(step.Assignment.Target))
            .FirstOrDefault();
        var addsRecordedOrStacks = addBody.Returns.Any(ret =>
            (ret.Value is Field f && InternalDataField(f)?.FieldType == typeof(int))
            || IsStackAmount(ret.Value));
        var subtractStep = afterBody.Calls.FirstOrDefault(step => step.Call.Method.DeclaringType == typeof(PowerCmd)
            && step.Call.Method.Name == nameof(PowerCmd.ModifyAmount)
            && step.Call.Arguments.Length >= 3
            && step.Call.Arguments[1] is Input { Name: "this" }
            && step.Call.Arguments[2] is Call { Method.Name: "op_Implicit", Arguments: [Operation { Operator: "neg", Left: Field recorded }] }
            && InternalDataField(recorded)?.FieldType == typeof(int));
        var checksPoweredBlock = addBody.Returns.Where(r => Numeric(r.Value) != 0m).All(r =>
            r.Conditions.Any(c => NormalizeCondition(c) is { Expected: true, Value: Call { Method.Name: "IsPoweredCardOrMonsterMoveBlock" } }));
        if (recordCardPlayStep is null || !HasCardPlayOwnerCheck(recordCardPlayStep.Conditions)
            || !recordsAmount || gaveBonusField is null || !addsRecordedOrStacks
            || subtractStep is null || !checksPoweredBlock)
            return false;

        var subtractChecksBonus = subtractStep.Conditions.Any(c =>
            NormalizeCondition(c) is { Expected: true, Value: Field f } && Equals(InternalDataField(f), gaveBonusField));
        if (!subtractChecksBonus)
            return false;

        line = $"{prefix}Next Block-gaining card bonus: adds +stacks Block (before multipliers) to owner's next powered Block-gaining card; if that card granted powered Block, subtracts the stacks present when it started after it resolves";
        return true;
    }

    private bool ReadQuietusHooks(Type power, string prefix, out string line)
    {
        line = "";
        if (FindOverride(power, "BeforeAttack") is not { } before
            || FindOverride(power, "ModifyDamageMultiplicative") is not { } mult
            || FindOverride(power, "AfterAttack") is not { } after
            || Body(before) is not { Incomplete: false } beforeBody
            || Body(mult) is not { Incomplete: false } multBody
            || Body(after) is not { Incomplete: false } afterBody)
            return false;

        var cmdStep = beforeBody.FieldSteps.FirstOrDefault(step => InternalDataField(step.Assignment.Target)?.FieldType == typeof(AttackCommand)
            && step.Assignment.Assigned is Input { Name: "command" });
        var cmdField = cmdStep is not null ? InternalDataField(cmdStep.Assignment.Target) : null;
        var nonNeutralReturns = multBody.Returns.Where(ret => Numeric(ret.Value) != 1m).ToArray();
        var multipliesByStacks = nonNeutralReturns.Length > 0 && nonNeutralReturns.All(ret => IsStackAmount(ret.Value));
        var removeStep = afterBody.Calls.FirstOrDefault(step => step.Call.Method.DeclaringType == typeof(PowerCmd)
            && step.Call.Method.Name == nameof(PowerCmd.Remove)
            && step.Call.Arguments is [Input { Name: "this" }]);
        if (cmdStep is null || cmdField is null || !multipliesByStacks || removeStep is null)
            return false;

        var beforeChecksAttacker = cmdStep.Conditions.Any(c =>
            IsEquality(NormalizeCondition(c), true, out var l, out var r)
            && ((l is Call { Method.Name: "get_Attacker", Receiver: Input { Name: "command" } } && IsOwner(r))
                || (r is Call { Method.Name: "get_Attacker", Receiver: Input { Name: "command" } } && IsOwner(l))));
        var multChecksDealerAndPowered = nonNeutralReturns.All(r =>
            HasPoweredAttackCheck(r.Conditions)
            && r.Conditions.Any(c =>
                IsEquality(NormalizeCondition(c), true, out var l, out var r2)
                && ((l is Input { Name: "dealer" } && IsOwner(r2)) || (r2 is Input { Name: "dealer" } && IsOwner(l)))));
        var removeChecksRecordedCmd = removeStep.Conditions.Any(c =>
            IsEquality(NormalizeCondition(c), true, out var l, out var r)
            && ((l is Input { Name: "command" } && r is Field rf && Equals(InternalDataField(rf), cmdField))
                || (r is Input { Name: "command" } && l is Field lf && Equals(InternalDataField(lf), cmdField))));
        if (!beforeChecksAttacker || !HasPoweredAttackCheck(cmdStep.Conditions)
            || !multChecksDealerAndPowered || !removeChecksRecordedCmd)
            return false;

        var beforeFlags = ExtractRequiredValuePropFlags(cmdStep.Conditions);
        if (beforeFlags is null || beforeFlags.Any(f => f != ValueProp.FromArrow))
            return false;
        var allMultFlags = nonNeutralReturns.Select(r => ExtractRequiredValuePropFlags(r.Conditions)).ToArray();
        if (allMultFlags.Any(f => f is null || !f.SetEquals(beforeFlags)))
            return false;

        var attackKind = beforeFlags.Contains(ValueProp.FromArrow)
            ? "powered arrow attack"
            : "powered attack";
        line = $"{prefix}Next {attackKind} multiplier: multiplies damage per hit by ×stacks (after additions) on owner's first {attackKind}, then removes this power after the entire attack resolves";
        return true;
    }

    private bool ReadEncoreHooks(Type power, string prefix, out string line)
    {
        line = "";
        if (FindOverride(power, "AfterApplied") is not { } applied
            || FindOverride(power, "BeforeCardPlayed") is not { } before
            || FindOverride(power, "ShouldPlay") is not { } shouldPlay
            || FindOverride(power, "AfterCardPlayed") is not { } after
            || Body(applied) is not { Incomplete: false } appliedBody
            || Body(before) is not { Incomplete: false } beforeBody
            || Body(shouldPlay) is not { Incomplete: false } playBody
            || Body(after) is not { Incomplete: false } afterBody)
            return false;

        if (!IsDisplayVarSyncOnly(appliedBody))
            return false;

        var trackStep = beforeBody.Calls.FirstOrDefault(s => s.Call.Method.Name == "Add"
            && s.Call.Receiver is Field f && InternalDataField(f)?.FieldType == typeof(List<CardModel>));
        var incrementStep = afterBody.FieldSteps.FirstOrDefault(s => InternalDataField(s.Assignment.Target)?.FieldType == typeof(int)
            && s.Assignment.Assigned is Operation { Operator: "add", Left: Field fl, Right: Constant { Data: 1 } }
            && Equals(InternalDataField(fl), InternalDataField(s.Assignment.Target)));
        var limitReturn = playBody.Returns.FirstOrDefault(r =>
            r.Value is Operation { Operator: "clt", Left: Operation { Operator: "add", Left: Field lf, Right: Call { Method.Name: "get_Count" } }, Right: var rhs }
            && InternalDataField(lf)?.FieldType == typeof(int) && IsStackAmount(rhs));
        if (trackStep is null || incrementStep is null || limitReturn is null
            || !HasCardPlayOwnerCheck(trackStep.Conditions)
            || !HasCardPlayOwnerCheck(incrementStep.Conditions)
            || !limitReturn.Conditions.Any(c => IsCardOwnerCheck(NormalizeCondition(c), "card")))
            return false;

        line = $"{prefix}Card play limit: prevent owner from starting another card play once cards played since this power was applied (including cards currently resolving) reach stacks";
        return true;
    }

    private bool ReadImpaledHooks(Type power, string prefix, out List<string> lines)
    {
        lines = [];
        if (FindOverride(power, "AfterApplied") is not { } applied
            || FindOverride(power, "AfterDamageReceived") is not { } hit
            || FindOverride(power, "AfterDeath") is not { } death
            || Body(applied) is not { Incomplete: false } appliedBody
            || Body(hit) is not { Incomplete: false } hitBody
            || Body(death) is not { Incomplete: false } deathBody)
            return false;

        if (!IsDisplayVarSyncOnly(appliedBody))
            return false;

        if (TryGetCanonicalPower(power) is not { } canonical
            || !canonical.DynamicVars.TryGetValue("HitCounter", out var hitCounterVar))
            return false;

        var dislodgeStep = hitBody.Calls.FirstOrDefault(s => s.Call.Method.DeclaringType == typeof(ArrowCmd)
            && s.Call.Method.Name == nameof(ArrowCmd.DislodgeArrow));
        var removesWhenEmpty = hitBody.Calls.Any(s => s.Call.Method.DeclaringType == power
            && s.Call.Method.Name == "RemoveIfNoArrows");
        var dislodgeAllStep = deathBody.Calls.FirstOrDefault(s => s.Call.Method.DeclaringType == power
            && s.Call.Method.Name == "DislodgeAllArrows");
        var removesOnDeath = deathBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(PowerCmd)
            && s.Call.Method.Name == nameof(PowerCmd.Remove)
            && s.Call.Arguments is [Input { Name: "this" }]);
        if (dislodgeStep is null || !removesWhenEmpty || dislodgeAllStep is null || !removesOnDeath)
            return false;

        var hitChecksOwner = dislodgeStep.Conditions.Any(c =>
            IsEquality(NormalizeCondition(c), true, out var l, out var r)
            && ((l is Input { Name: "target" } && IsOwner(r)) || (r is Input { Name: "target" } && IsOwner(l))));
        var hitChecksApplier = dislodgeStep.Conditions.Any(c =>
            IsEquality(NormalizeCondition(c), true, out var l, out var r)
            && ((l is Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_Owner", Receiver: Input { Name: "source" } } }
                    && r is Call { Method.Name: "get_Applier" })
                || (r is Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_Owner", Receiver: Input { Name: "source" } } }
                    && l is Call { Method.Name: "get_Applier" })));
        var resetsToBase = hitBody.Calls.Any(s =>
            s.Call.Method.Name == "set_HitCounter"
            && s.Call.Arguments is [var val]
            && Numeric(val) == hitCounterVar.BaseValue);
        var deathChecksOwner = dislodgeAllStep.Conditions.Any(c =>
            IsEquality(NormalizeCondition(c), true, out var l, out var r)
            && ((l is Input { Name: "creature" } && IsOwner(r)) || (r is Input { Name: "creature" } && IsOwner(l))));
        var deathChecksApplierAlive = dislodgeAllStep.Conditions.Any(c =>
            NormalizeCondition(c) is { Expected: false, Value: Call { Method.Name: "get_IsDead", Receiver: Call { Method.Name: "get_Applier" } } });
        if (!hitChecksOwner || !HasNonNullCheck(dislodgeStep.Conditions, "source")
            || !hitChecksApplier || !HasPoweredAttackCheck(dislodgeStep.Conditions)
            || !resetsToBase || !deathChecksOwner || !deathChecksApplierAlive)
            return false;

        var hitsNeeded = Number(hitCounterVar.BaseValue);
        lines.Add($"{prefix}Impaled arrows: hit counter starts at {hitsNeeded} (shown as display_amount); after each powered card attack hit from the applier (including fully blocked hits), the counter decreases by 1, and on reaching 0 resets to {hitsNeeded}, dislodges 1 arrow back to the applier, and removes this power if no impaled arrows remain");
        lines.Add($"{prefix}On owner death: if applier is alive, dislodge all impaled arrows back to the applier and remove this power");
        return true;
    }

    private bool ReadSimpletonHook(Type power, string prefix, out string line)
    {
        line = "";
        if (FindOverride(power, "AfterAutoPostPlayPhaseEntered") is not { } hook
            || Body(hook) is not { Incomplete: false } body)
            return false;

        var autoPlay = body.Calls.FirstOrDefault(s =>
            s.Call.Method.DeclaringType == typeof(CardCmd) && s.Call.Method.Name == nameof(CardCmd.AutoPlay));
        if (autoPlay is null || !HasPlayerOwnerCheck(autoPlay.Conditions))
            return false;
        var hasStackLoop = body.Loops.Any(l => autoPlay.Index >= l.Start && autoPlay.Index < l.End && IsStackAmount(l.Count));
        var getsHandPile = body.Calls.Any(s =>
            s.Call.Method.Name == "GetPile"
            && s.Call.Arguments.Length >= 1
            && Numeric(s.Call.Arguments[0]) == (decimal)PileType.Hand);
        var filtersNonUnplayable = body.Calls.Any(s =>
            s.Call.Method.Name == nameof(Enumerable.Where)
            && TryExtractDelegatePredicate(s.Call, out var pred) && pred is not null
            && IsNonUnplayableCardPredicate(pred));
        var groupsByEnergy = body.Calls.Any(s => s.Call.Method.Name == nameof(Enumerable.GroupBy))
            && body.Calls.Any(s => s.Call.Method.Name == nameof(Enumerable.OrderBy));
        if (!hasStackLoop || !getsHandPile || !filtersNonUnplayable || !groupsByEnergy)
            return false;

        line = $"{prefix}At end of turn before hand discard: repeats stacks times: auto-play a random non-Unplayable card among the lowest current Energy cost cards in owner's hand (Energy X-cost cards count as highest cost; auto-play ignores Energy cost)";
        return true;
    }

    private bool IsNonUnplayableCardPredicate(MethodInfo predicate)
    {
        var body = Body(predicate);
        if (body.Incomplete || body.Returns.Count != 1)
            return false;
        var ret = NormalizeCondition(new Test(body.Returns[0].Value, true));
        return !ret.Expected
            && ret.Value is Call
            {
                Method.Name: "Contains",
                Receiver: Call { Method.Name: "get_Keywords" },
                Arguments: [Constant { Data: (int)CardKeyword.Unplayable }],
            } or Call
            {
                Method.Name: "Contains",
                Arguments: [Call { Method.Name: "get_Keywords" }, Constant { Data: (int)CardKeyword.Unplayable }],
            };
    }

    private bool ReadMelodyFirstPlayCountHook(Type power, string prefix, out string line)
    {
        line = "";
        if (FindOverride(power, "AfterCardPlayed") is not { } hook
            || Body(hook) is not { Incomplete: false } body)
            return false;

        // Verify all three Melody tags (Wah, Cho, Ki) are checked on cardPlay.Card.Tags
        // AND counted for first play this turn in CombatManager.Instance.History.CardPlaysStarted.
        var checkedTags = new HashSet<CardTag>();
        var countedFirstTags = new HashSet<CardTag>();
        foreach (var call in body.Calls)
        {
            if (call.Call.Method.Name == "Contains"
                && call.Call.Arguments is [Call { Method.Name: "get_Tags" }, Constant { Data: int rawTag }]
                && Enum.IsDefined(typeof(CardTag), rawTag))
            {
                checkedTags.Add((CardTag)rawTag);
            }
            if (call.Call.Method.Name == nameof(Enumerable.Count)
                && call.Call.Arguments.Length == 2
                && call.Call.Arguments[0] is Call { Method.Name: "get_CardPlaysStarted" }
                && TryExtractDelegatePredicate(call.Call, out var pred) && pred is not null
                && TryReadOwnerMelodyHistoryPredicate(pred, out var histTag))
            {
                countedFirstTags.Add(histTag);
            }
        }
        var required = new[] { CardTag.Wah, CardTag.Cho, CardTag.Ki };
        if (!checkedTags.IsSupersetOf(required) || !countedFirstTags.IsSupersetOf(required))
            return false;

        var drawStep = body.Calls.FirstOrDefault(s =>
            s.Call.Method.DeclaringType == typeof(CardPileCmd) && s.Call.Method.Name == nameof(CardPileCmd.Draw));
        if (drawStep is not null)
        {
            var countArg = Argument(drawStep.Call, "count") ?? drawStep.Call.Arguments.ElementAtOrDefault(1);
            if (!HasCardPlayOwnerCheck(drawStep.Conditions) || !IsMultipliedByStacks(countArg)
                || !HasFirstPlayCountEqualsOne(body, countArg, drawStep.Conditions))
                return false;
            line = $"{prefix}After each owned card play resolves: for each Melody tag (Wah/Cho/Ki) on the card that was played for the first time this turn, owner draws stacks card(s)";
            return true;
        }

        var autoPlayStep = body.Calls.FirstOrDefault(s =>
            s.Call.Method.DeclaringType == typeof(CardPileCmd) && s.Call.Method.Name == nameof(CardPileCmd.AutoPlayFromDrawPile));
        if (autoPlayStep is not null)
        {
            var countArg = Argument(autoPlayStep.Call, "count") ?? autoPlayStep.Call.Arguments.ElementAtOrDefault(2);
            if (!HasCardPlayOwnerCheck(autoPlayStep.Conditions) || !IsMultipliedByStacks(countArg)
                || !HasFirstPlayCountEqualsOne(body, countArg, autoPlayStep.Conditions))
                return false;
            var posArg = Argument(autoPlayStep.Call, "position") ?? autoPlayStep.Call.Arguments.ElementAtOrDefault(3);
            var posLabel = Numeric(posArg) is { } rawPos ? ((CardPilePosition)(int)rawPos).ToString().ToLowerInvariant() + " " : "";
            var forceExhaust = Argument(autoPlayStep.Call, "forceExhaust") ?? autoPlayStep.Call.Arguments.ElementAtOrDefault(4);
            var exhaustNote = forceExhaust is Constant { Data: 1 or true } ? " (exhausting after play)" : "";
            line = $"{prefix}After each owned card play resolves: for each Melody tag (Wah/Cho/Ki) on the card that was played for the first time this turn, auto-play the {posLabel}stacks card(s) of owner's draw pile{exhaustNote}";
            return true;
        }

        return false;
    }

    private bool HasFirstPlayCountEqualsOne(NativeRuleBody body, Value? countArg, Test[] conditions)
    {
        // `bool flag = History.CardPlaysStarted.Count(...) == 1; num += (flag ? 1 : 0);`
        // appears either as a `ceq(Count(...), 1)` inside the accumulated count expression
        // or as an equality condition on the branching paths.
        var matched = new HashSet<CardTag>();
        CollectCountEqualsOneTags(countArg, matched);
        foreach (var cond in conditions)
            CollectCountEqualsOneTags(cond.Value, matched);
        foreach (var terminal in body.Terminals)
            foreach (var cond in terminal.Conditions)
                CollectCountEqualsOneTags(cond.Value, matched);
        return matched.IsSupersetOf([CardTag.Wah, CardTag.Cho, CardTag.Ki]);
    }

    private void CollectCountEqualsOneTags(Value? expr, HashSet<CardTag> matched)
    {
        switch (expr)
        {
            case Operation { Operator: "ceq" or "beq" or "bne.un", Left: var l, Right: var r }:
                if (l is Call { Method.Name: nameof(Enumerable.Count), Arguments: [Call { Method.Name: "get_CardPlaysStarted" }, _] } countL
                    && Numeric(r) == 1m
                    && TryExtractDelegatePredicate(countL, out var predL) && predL is not null
                    && TryReadOwnerMelodyHistoryPredicate(predL, out var tagL))
                {
                    matched.Add(tagL);
                }
                else if (r is Call { Method.Name: nameof(Enumerable.Count), Arguments: [Call { Method.Name: "get_CardPlaysStarted" }, _] } countR
                    && Numeric(l) == 1m
                    && TryExtractDelegatePredicate(countR, out var predR) && predR is not null
                    && TryReadOwnerMelodyHistoryPredicate(predR, out var tagR))
                {
                    matched.Add(tagR);
                }
                CollectCountEqualsOneTags(l, matched);
                CollectCountEqualsOneTags(r, matched);
                break;
            case Operation op:
                CollectCountEqualsOneTags(op.Left, matched);
                CollectCountEqualsOneTags(op.Right, matched);
                break;
            case Call c:
                CollectCountEqualsOneTags(c.Receiver, matched);
                foreach (var arg in c.Arguments)
                    CollectCountEqualsOneTags(arg, matched);
                break;
        }
    }

    private bool TryReadOwnerMelodyHistoryPredicate(MethodInfo predicate, out CardTag tag)
    {
        tag = default;
        var body = Body(predicate);
        if (body.Incomplete || body.Returns.Count == 0)
            return false;
        var positive = body.Returns.Where(r => !(TryTruth(r.Value, out var b) && !b)).ToArray();
        if (positive.Length != 1)
            return false;
        var tests = new List<Test>(positive[0].Conditions);
        if (!(TryTruth(positive[0].Value, out var retBool) && retBool))
            tests.Add(new Test(positive[0].Value, true));

        var hasTurn = false;
        var hasOwner = false;
        CardTag? foundTag = null;
        foreach (var raw in tests)
        {
            var c = NormalizeCondition(raw);
            if (c.Expected && c.Value is Call { Method.Name: "HappenedThisTurn" })
            {
                hasTurn = true;
                continue;
            }
            if (IsEquality(c, true, out var l, out var r)
                && ((IsEntryOwnerExpr(l) && IsOwnerPlayerOrCreature(r))
                    || (IsEntryOwnerExpr(r) && IsOwnerPlayerOrCreature(l))))
            {
                hasOwner = true;
                continue;
            }
            if (c.Expected && c.Value is Call { Method.Name: "Contains", Arguments: [Call { Method.Name: "get_Tags" }, Constant { Data: int rawTag }] }
                && Enum.IsDefined(typeof(CardTag), rawTag))
            {
                foundTag = (CardTag)rawTag;
                continue;
            }
            return false;
        }
        if (!hasTurn || !hasOwner || foundTag is null)
            return false;
        tag = foundTag.Value;
        return true;
    }

    private static bool IsMultipliedByStacks(Value? expr) => expr switch
    {
        Call { Method.Name: "op_Implicit", Arguments: [var inner] } => IsMultipliedByStacks(inner),
        Operation { Operator: "mul", Left: var l, Right: var r } => IsStackAmount(l) || IsStackAmount(r),
        _ => false,
    };

    private bool ReadEnsembleHooks(Type power, string prefix, out List<string> lines)
    {
        lines = [];
        if (FindOverride(power, "TryModifyEnergyCostInCombatLate") is not { } costHook
            || FindOverride(power, "AfterCardPlayed") is not { } afterHook
            || Body(costHook) is not { Incomplete: false } costBody
            || Body(afterHook) is not { Incomplete: false } afterBody)
            return false;

        var shouldSkipCall = costBody.Calls.FirstOrDefault(s => s.Call.Method.DeclaringType == power && s.Call.Method.Name == "ShouldSkip");
        if (shouldSkipCall?.Call.Method is not MethodInfo shouldSkipMethod
            || !TryReadEnsembleShouldSkip(shouldSkipMethod, out var allowedPiles))
            return false;

        var trueReturns = costBody.Returns.Where(r => TryTruth(r.Value, out var b) && b).ToArray();
        if (trueReturns.Length == 0)
            return false;
        var zeroCostStores = costBody.OutStores.Where(s => s.Parameter.Name == "modifiedCost" && Numeric(s.Assigned) == 0m).ToArray();
        if (zeroCostStores.Length == 0 || !trueReturns.All(ret => zeroCostStores.Any(st => st.Conditions.All(c => ret.Conditions.Contains(c)))))
            return false;

        var decrementStep = afterBody.Calls.FirstOrDefault(s =>
            s.Call.Method.DeclaringType == typeof(PowerCmd) && s.Call.Method.Name == nameof(PowerCmd.Decrement)
            && s.Call.Arguments is [Input { Name: "this" }]);
        if (decrementStep is null
            || !HasCardPlayOwnerCheck(decrementStep.Conditions, expected: false)
            || !decrementStep.Conditions.Any(c => NormalizeCondition(c) is { Expected: true, Value: Call { Method.Name: "get_IsLastInSeries" } }))
            return false;

        var pileList = string.Join(" or ", allowedPiles.OrderBy(p => p).Select(p => p.ToString()));
        lines.Add($"{prefix}In-combat Energy cost (late): teammates' cards (not owner's) in {pileList} cost 0 Energy (Energy X-cost cards ignore cost modifiers)");
        lines.Add($"{prefix}After each card play resolves: if played by a teammate and last in play series: decrement stacks by 1");
        return true;
    }

    private bool TryReadEnsembleShouldSkip(MethodInfo shouldSkip, out HashSet<PileType> allowedPiles)
    {
        allowedPiles = [];
        var body = Body(shouldSkip);
        if (body.Incomplete || body.Returns.Count == 0)
            return false;
        var activeReturns = body.Returns.Where(r => TryTruth(r.Value, out var skip) && !skip).ToArray();
        if (activeReturns.Length == 0)
            return false;

        var reachable = 0;
        foreach (var ret in activeReturns)
        {
            if (IsUnreachableNullablePath(ret.Conditions))
                continue;
            reachable++;
            var excludesOwner = false;
            var foundPile = false;
            foreach (var raw in ret.Conditions)
            {
                var c = NormalizeCondition(raw);
                if (IsEquality(c, false, out var ownerL, out var ownerR)
                    && ((ownerL is Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_Owner", Receiver: Input { Name: "card" } } } && IsOwner(ownerR))
                        || (ownerR is Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_Owner", Receiver: Input { Name: "card" } } } && IsOwner(ownerL))))
                {
                    excludesOwner = true;
                    continue;
                }
                if (c.Expected && c.Value is Call { Method.Name: "get_Pile", Receiver: Input { Name: "card" } })
                    continue;
                if (c.Value is Call { Method.Name: "get_HasValue", Method.DeclaringType: { IsGenericType: true } nullableDecl } hvCall
                    && nullableDecl.GetGenericTypeDefinition() == typeof(Nullable<>))
                {
                    if ((c.Expected && hvCall.Receiver is Call { Method: ConstructorInfo })
                        || (!c.Expected && hvCall.Receiver is Constant { Data: 0 }))
                        continue;
                    return false;
                }
                if (IsEquality(c, true, out var l, out var r)
                    && l is not null && UnwrapPileTypeExpr(l) is Input { Name: "card" }
                    && Numeric(r) is { } rawPile && Enum.IsDefined(typeof(PileType), (int)rawPile))
                {
                    allowedPiles.Add((PileType)(int)rawPile);
                    foundPile = true;
                    continue;
                }
                if (IsEquality(c, false, out var nl, out var nr)
                    && nl is not null && UnwrapPileTypeExpr(nl) is Input { Name: "card" }
                    && Numeric(nr) is { } rawNotPile && Enum.IsDefined(typeof(PileType), (int)rawNotPile))
                {
                    // Short-circuit fallthrough from an earlier switch case.
                    continue;
                }
                if (c.Value is Operation
                    {
                        Operator: "bgt.un" or "cgt.un" or "ble.un" or "cle.un",
                        Left: Operation { Operator: "sub", Left: var pileExpr, Right: Constant { Data: int offset } },
                        Right: Constant { Data: int span },
                    } op
                    && UnwrapPileTypeExpr(pileExpr) is Input { Name: "card" })
                {
                    var inRange = op.Operator is "ble.un" or "cle.un" ? c.Expected : !c.Expected;
                    if (!inRange || span < 0 || span > 4)
                        return false;
                    for (var v = offset; v <= offset + span; v++)
                    {
                        if (!Enum.IsDefined(typeof(PileType), v))
                            return false;
                        allowedPiles.Add((PileType)v);
                    }
                    foundPile = true;
                    continue;
                }
                // Fail closed on any unrecognized guard in ShouldSkip.
                return false;
            }
            if (!excludesOwner || !foundPile)
                return false;
        }
        return reachable > 0 && allowedPiles.Count > 0;
    }

    private bool ReadTeammateDeathEnergyHook(Type power, string prefix, out string line)
    {
        line = "";
        if (FindOverride(power, "AfterDeath") is not { } hook
            || Body(hook) is not { Incomplete: false } body)
            return false;

        var gainStep = body.Calls.FirstOrDefault(s =>
            s.Call.Method.DeclaringType == typeof(PlayerCmd) && s.Call.Method.Name == nameof(PlayerCmd.GainEnergy)
            && s.Call.Arguments.Length >= 2 && IsStackAmount(s.Call.Arguments[0]));
        var teammatesWhere = body.Calls.FirstOrDefault(s =>
            s.Call.Method.Name == nameof(Enumerable.Where)
            && s.Call.Arguments.Length == 2
            && s.Call.Arguments[0] is Call { Method.Name: "GetTeammatesOf" }
            && TryExtractDelegatePredicate(s.Call, out var pred) && pred is not null
            && IsLivingTeammatePlayersPredicate(pred, mustExcludeOwner: false));
        if (gainStep is null || teammatesWhere is null || !HasEnemySideCheck(gainStep.Conditions, "target"))
            return false;

        line = $"{prefix}On enemy death: each living player teammate (including owner) gains stacks Energy";
        return true;
    }

    private bool ReadCoveringFireHook(Type power, string prefix, out string line)
    {
        line = "";
        if (FindOverride(power, "AfterArrowShot") is not { } hook
            || Body(hook) is not { Incomplete: false } body)
            return false;

        var blockStep = body.Calls.FirstOrDefault(s =>
            s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.GainBlock)
            && s.Call.Arguments.Length >= 3 && IsStackAmount(s.Call.Arguments[1])
            && Numeric(s.Call.Arguments[2]) is { } rawProp && ((ValueProp)(int)rawProp).HasFlag(ValueProp.Unpowered));
        var teammatesWhere = body.Calls.FirstOrDefault(s =>
            s.Call.Method.Name == nameof(Enumerable.Where)
            && s.Call.Arguments.Length == 2
            && s.Call.Arguments[0] is Call { Method.Name: "GetTeammatesOf" }
            && TryExtractDelegatePredicate(s.Call, out var pred) && pred is not null
            && IsLivingTeammatePlayersPredicate(pred, mustExcludeOwner: true));
        if (blockStep is null || teammatesWhere is null
            || !HasPlayerOwnerCheck(blockStep.Conditions, "owner")
            || !HasNonNullCheck(blockStep.Conditions, "source"))
            return false;

        line = $"{prefix}After owner shoots an arrow from a card: each other living player teammate gains stacks unpowered Block";
        return true;
    }

    private bool ReadEcstasyHook(Type power, string prefix, out string line)
    {
        line = "";
        if (FindOverride(power, "AfterPlayerTurnStart") is not { } hook
            || Body(hook) is not { Incomplete: false } body)
            return false;

        var transformStep = body.Calls.FirstOrDefault(s => s.Call.Method.DeclaringType == typeof(CardCmd)
            && s.Call.Method.Name == nameof(CardCmd.Transform));
        var ofTypeCall = body.Calls.FirstOrDefault(s => s.Call.Method.Name == nameof(Enumerable.OfType) && s.Call.Method.IsGenericMethod);
        var createCall = body.Calls.FirstOrDefault(s => s.Call.Method.Name == "CreateCard" && s.Call.Method.IsGenericMethod);
        var shuffles = body.Calls.Any(s => s.Call.Method.Name == "StableShuffle");
        var takesStacks = body.Calls.Any(s =>
            s.Call.Method.Name == nameof(Enumerable.Take)
            && s.Call.Arguments.Length >= 2 && IsStackAmount(s.Call.Arguments[1]));
        if (transformStep is null || ofTypeCall is null || createCall is null
            || !TryReadOwnerPileLabel(body, out var pileLabel) || !shuffles || !takesStacks
            || !HasPlayerOwnerCheck(transformStep.Conditions))
            return false;

        var fromType = ofTypeCall.Call.Method.GetGenericArguments()[0];
        var toType = createCall.Call.Method.GetGenericArguments()[0];
        if (!typeof(CardModel).IsAssignableFrom(fromType) || !typeof(CardModel).IsAssignableFrom(toType))
            return false;

        _generatedCards.Add(fromType);
        _generatedCards.Add(toType);
        line = $"{prefix}After owner's turn starts: transform up to stacks random {ModelLabel(fromType)} card(s) in {pileLabel} into {ModelLabel(toType)}";
        return true;
    }

    private bool ReadEnnuiBeforeHandDrawHook(Type power, string prefix, out string line)
    {
        line = "";
        if (FindOverride(power, "BeforeHandDraw") is not { } hook
            || Body(hook) is not { Incomplete: false } body)
            return false;

        var addStep = body.Calls.FirstOrDefault(s =>
            s.Call.Method.DeclaringType == typeof(CardPileCmd) && s.Call.Method.Name == nameof(CardPileCmd.AddGeneratedCardsToCombat));
        var createStep = body.Calls.FirstOrDefault(s =>
            s.Call.Method.DeclaringType == typeof(ICombatState) && s.Call.Method.Name == "CreateCard" && s.Call.Method.IsGenericMethod);
        if (addStep is null || createStep is null
            || !HasPlayerOwnerCheck(addStep.Conditions)
            || !TryReadGeneratedPile(addStep.Call, out var pile, out var overflowNote))
            return false;

        var cardType = createStep.Call.Method.GetGenericArguments()[0];
        if (!typeof(CardModel).IsAssignableFrom(cardType))
            return false;

        var hasCardsVarLoop = body.Loops.Any(l =>
            createStep.Index >= l.Start && createStep.Index < l.End
            && IsCardsDynamicVarBaseValue(l.Count));
        if (!hasCardsVarLoop)
            return false;

        if (TryGetCanonicalPower(power) is not { } canonical
            || !canonical.DynamicVars.TryGetValue("Cards", out var cardsVar)
            || cardsVar.BaseValue != 0m)
            return false;

        var hasIncrementer = power.GetMethods(BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly)
            .Any(m => m.GetParameters().Length == 0 && !m.IsSpecialName
                && Body(m) is { Incomplete: false } incBody
                && incBody.Calls.Any(s => s.Call.Method.Name == "set_BaseValue"
                    && s.Call.Receiver is Call { Method.Name: "get_Cards", Receiver: Call { Method.Name: "get_DynamicVars" } }));
        if (!hasIncrementer)
            return false;

        _generatedCards.Add(cardType);
        line = $"{prefix}Before turn-start hand draw: if player=owner: add 1 copy of {ModelLabel(cardType)} per time this power was applied to owner's {pile.ToString().ToLowerInvariant()} pile{overflowNote}";
        return true;
    }

    private static bool IsCardsDynamicVarBaseValue(Value? value) => value switch
    {
        Call { Method.Name: "op_Implicit" or "op_Explicit", Arguments: [var inner] } => IsCardsDynamicVarBaseValue(inner),
        Call { Method.Name: "get_BaseValue", Receiver: Call { Method.Name: "get_Cards", Receiver: Call { Method.Name: "get_DynamicVars" } } } => true,
        _ => false,
    };

    private bool ReadMythicFormHook(Type power, string prefix, out string line)
    {
        line = "";
        if (FindOverride(power, "BeforeHandDraw") is not { } hook
            || Body(hook) is not { Incomplete: false } body)
            return false;

        var addStep = body.Calls.FirstOrDefault(s =>
            s.Call.Method.DeclaringType == typeof(CardPileCmd) && s.Call.Method.Name == nameof(CardPileCmd.AddGeneratedCardToCombat));
        var usesTarot = body.Calls.Any(s => s.Call.Method.DeclaringType?.Name == "TarotCards" && s.Call.Method.Name == "get_All");
        if (addStep is null || !usesTarot
            || !HasPlayerOwnerCheck(addStep.Conditions)
            || !TryReadGeneratedPile(addStep.Call, out var pile, out var overflowNote))
            return false;

        var hasStackLoop = body.Loops.Any(l => addStep.Index >= l.Start && addStep.Index < l.End && IsStackAmount(l.Count));
        if (!hasStackLoop)
            return false;

        line = $"{prefix}Before turn-start hand draw: if player=owner: add stacks random run-allowed Tarot card(s) to owner's {pile.ToString().ToLowerInvariant()} pile{overflowNote}";
        return true;
    }

    private bool ReadShowOffDeathHook(Type power, string prefix, out string line)
    {
        line = "";
        if (FindOverride(power, "AfterDeath") is not { } hook
            || Body(hook) is not { Incomplete: false } body)
            return false;

        var increaseAim = body.Calls.FirstOrDefault(s =>
            s.Call.Method.DeclaringType == typeof(ShowOff) && s.Call.Method.Name == nameof(ShowOff.IncreaseAim)
            && s.Call.Arguments.Length == 1 && IsStackAmount(s.Call.Arguments[0]));
        var removesSelf = body.Calls.Any(s =>
            s.Call.Method.DeclaringType == typeof(PowerCmd) && s.Call.Method.Name == nameof(PowerCmd.Remove)
            && s.Call.Arguments is [Input { Name: "this" }]);
        if (increaseAim is null || !removesSelf || !HasEnemySideCheck(increaseAim.Conditions, "target"))
            return false;

        var checksFatal = increaseAim.Conditions.Any(c =>
            NormalizeCondition(c) is { Expected: true, Value: Call { Method.Name: nameof(Enumerable.All) } allCall }
            && TryExtractDelegatePredicate(allCall, out var pred) && pred is not null
            && Body(pred) is { Incomplete: false, Returns: [{ Value: Call { Method.Name: "ShouldOwnerDeathTriggerFatal" } }] });
        if (!checksFatal)
            return false;

        line = $"{prefix}On fatal enemy death: permanently increase the source {ModelLabel(typeof(ShowOff))} card's {ModelLabel(typeof(AimPower))} by stacks and remove this power";
        return true;
    }

    private bool ReadTuneHook(Type power, string prefix, out string line)
    {
        line = "";
        if (FindOverride(power, "AfterCardPlayed") is not { } hook
            || Body(hook) is not { Incomplete: false } body)
            return false;

        var upgradeStep = body.Calls.FirstOrDefault(s =>
            s.Call.Method.DeclaringType == typeof(CardCmd) && s.Call.Method.Name == nameof(CardCmd.Upgrade));
        var checksUpgradable = body.Calls.Any(s =>
            s.Call.Method.Name == nameof(Enumerable.Where)
            && TryExtractDelegatePredicate(s.Call, out var pred) && pred is not null
            && Body(pred) is { Incomplete: false, Returns: [{ Value: Call { Method.Name: "get_IsUpgradable" } }] });
        var takesRandomStacks = body.Calls.Any(s =>
            s.Call.Method.Name == "TakeRandom"
            && s.Call.Arguments.Length >= 2 && IsStackAmount(s.Call.Arguments[1]));
        if (upgradeStep is null || !TryReadOwnerPileLabel(body, out var pileLabel)
            || !checksUpgradable || !takesRandomStacks
            || !HasCardPlayOwnerCheck(upgradeStep.Conditions))
            return false;

        CardTag? matchedTag = null;
        foreach (var raw in upgradeStep.Conditions)
        {
            var c = NormalizeCondition(raw);
            if (c.Expected && c.Value is Call
                {
                    Method.DeclaringType: var decl,
                    Method.Name: nameof(MelodyCmd.IsFirstPlayThisTurn),
                    Arguments: [_, var tagArg],
                }
                && decl == typeof(MelodyCmd)
                && Numeric(tagArg) is { } rawTag && Enum.IsDefined(typeof(CardTag), (int)rawTag))
            {
                matchedTag = (CardTag)(int)rawTag;
                break;
            }
        }
        if (matchedTag is null)
            return false;

        var checksTag = upgradeStep.Conditions.Any(c =>
            NormalizeCondition(c) is { Expected: true, Value: Call { Method.Name: "Contains", Arguments: [Call { Method.Name: "get_Tags" }, Constant { Data: int rt }] } }
            && (CardTag)rt == matchedTag.Value);
        if (!checksTag)
            return false;

        line = $"{prefix}After each owned card play resolves: if card is {matchedTag.Value}-tagged and this is the first {matchedTag.Value}-tagged card play this turn: randomly upgrade up to stacks upgradable card(s) in {pileLabel}";
        return true;
    }

    private bool ReadBizarreLampHook(Type relic, string prefix, out string line)
    {
        line = "";
        if (FindOverride(relic, "BeforeHandDraw") is not { } hook
            || Body(hook) is not { Incomplete: false } body
            || body.Loops.Count > 0)
            return false;

        var addStep = body.Calls.FirstOrDefault(s =>
            s.Call.Method.DeclaringType == typeof(CardPileCmd) && s.Call.Method.Name == nameof(CardPileCmd.AddGeneratedCardsToCombat));
        if (addStep is null
            || !HasPlayerOwnerCheck(addStep.Conditions)
            || !HasTurnAtMostCheck(addStep.Conditions, out var maxTurn)
            || !TryReadGeneratedPile(addStep.Call, out var pile, out var overflowNote))
            return false;

        var createdTypes = body.Calls
            .Where(s => s.Call.Method.DeclaringType == typeof(ICombatState)
                && s.Call.Method.Name == "CreateCard" && s.Call.Method.IsGenericMethod)
            .Select(s => s.Call.Method.GetGenericArguments()[0])
            .Where(t => typeof(CardModel).IsAssignableFrom(t))
            .ToList();
        if (createdTypes.Count < 2)
            return false;

        foreach (var cardType in createdTypes)
            _generatedCards.Add(cardType);
        var cardList = string.Join(" and ", createdTypes.Select(t => $"1 {ModelLabel(t)}"));
        line = $"{prefix}Before turn-start hand draw: if player=owner and turn number≤{maxTurn}: add {cardList} to owner's {pile.ToString().ToLowerInvariant()} pile{overflowNote}";
        return true;
    }

    private bool ReadOcarinaHook(Type relic, string prefix, out string line)
    {
        line = "";
        if (FindOverride(relic, "BeforeHandDraw") is not { } hook
            || Body(hook) is not { Incomplete: false } body)
            return false;

        var addStep = body.Calls.FirstOrDefault(s =>
            s.Call.Method.DeclaringType == typeof(CardPileCmd) && s.Call.Method.Name == nameof(CardPileCmd.AddGeneratedCardsToCombat));
        var factoryStep = body.Calls.FirstOrDefault(s =>
            s.Call.Method.DeclaringType?.Name == "CardFactory" && s.Call.Method.Name == "GetDistinctForCombat");
        if (addStep is null || factoryStep is null
            || !HasPlayerOwnerCheck(addStep.Conditions)
            || !HasTurnAtMostCheck(addStep.Conditions, out var maxTurn)
            || factoryStep.Call.Arguments.Length < 3
            || ValueText(factoryStep.Call.Arguments[2]) is not { } countText
            || !TryReadGeneratedPile(addStep.Call, out var pile, out var overflowNote))
            return false;

        var hasMelodyField = relic.GetFields(BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public)
            .Any(f => f.FieldType == typeof(CardTag[]) && f.GetValue(null) is CardTag[] tags
                && tags.Contains(CardTag.Wah) && tags.Contains(CardTag.Cho) && tags.Contains(CardTag.Ki));
        if (!hasMelodyField)
            return false;

        line = $"{prefix}Before turn-start hand draw: if player=owner and turn number≤{maxTurn}: generate up to {countText} distinct random unlocked Melody-tagged (Wah/Cho/Ki) character card(s) into owner's {pile.ToString().ToLowerInvariant()} pile{overflowNote}";
        return true;
    }
}
