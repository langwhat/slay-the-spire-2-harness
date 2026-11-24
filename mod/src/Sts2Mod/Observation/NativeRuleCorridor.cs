using System.Collections;
using System.Globalization;
using System.Reflection;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Commands.Builders;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Powers;
using MegaCrit.Sts2.Core.Rooms;
using static Sts2Mod.Observation.NativeRuleBody;

namespace Sts2Mod.Observation;

internal sealed partial class NativeRuleText
{
    private Effect? DescribeCorridorEffect(Call call, MethodInfo caller, Type? power, out bool recognized)
    {
        recognized = false;
        var declaring = call.Method.DeclaringType;
        var name = call.Method.Name;

        // Ignore pre-spawn property configuration on newly created mutable monster instances;
        // CreatureCmd.Add inspects the same body to report gameplay-relevant spawn overrides.
        if (name.StartsWith("set_", StringComparison.Ordinal)
            && SummonedMonsterType(call.Receiver) is not null)
        {
            recognized = false;
            return null;
        }

        // Monster boolean flag mutation performed from an owned power hook (e.g., ImbalancedPower -> BowlbugRock, RavenousPower -> CorpseSlug).
        if (power is not null && name.StartsWith("set_", StringComparison.Ordinal)
            && call.Arguments is [Constant { Data: int flag }] && flag is 0 or 1
            && call.Method.GetParameters() is [{ ParameterType: var flagType }] && flagType == typeof(bool)
            && call.Receiver is Operation { Operator: "isinst" or "castclass",
                Left: Call { Method.Name: "get_Monster", Receiver: var ownerCreature },
                Right: Constant { Data: Type targetMonster } }
            && IsOwner(ownerCreature))
        {
            recognized = true;
            var propName = name[4..];
            if (BackingField(targetMonster, (MethodInfo)call.Method) is { } backing && !IsGameplayField(backing))
            {
                recognized = false;
                return null;
            }
            return new Effect($"{propName} = {(flag == 1 ? "true" : "false")}", ChangesGuard: true);
        }

        // Pool-based minion summon helper (e.g., Fabricator.SpawnBot(defenseSpawns / aggroSpawns)).
        if (call.Receiver == Self && call.Method is MethodInfo poolHelper && poolHelper.DeclaringType == caller.DeclaringType
            && poolHelper.ReturnType == typeof(Task)
            && call.Arguments is [Field { Receiver: null, Member: var poolField }]
            && DescribePoolSummonHelper(poolHelper, poolField) is { } poolEffect)
        {
            recognized = true;
            return poolEffect;
        }

        // GremlinMerc.Steal() forwarding to owned ThieveryPower instances.
        if (call.Receiver == Self && call.Method is MethodInfo stealHelper && stealHelper.Name == "Steal"
            && stealHelper.ReturnType == typeof(Task) && stealHelper.GetParameters().Length == 0
            && Body(stealHelper) is { Incomplete: false } stealBody
            && stealBody.Calls.Any(s => s.Call.Method.Name == "Steal" && s.Call.Method.DeclaringType == typeof(ThieveryPower)))
        {
            recognized = true;
            _powers.Add(typeof(ThieveryPower));
            return new Effect($"trigger each {PowerLabel(typeof(ThieveryPower))} on self to steal Gold from its target");
        }

        return null;
    }

    private Effect? DescribePoolSummonHelper(MethodInfo helper, FieldInfo poolField)
    {
        if (poolField.GetValue(null) is not IEnumerable rawPool)
            return null;
        var poolTypes = rawPool.OfType<MonsterModel>().Select(m => m.GetType())
            .OrderBy(t => ModelDb.GetId(t).ToString(), StringComparer.Ordinal).ToArray();
        if (poolTypes.Length == 0)
            return null;

        var body = Body(helper);
        if (body.Incomplete || body.Writes.Count > 0)
            return null;
        var addsMonster = body.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.Add));
        if (!addsMonster)
            return null;

        var excludesLast = body.FieldSteps.Count == 1 && body.FieldSteps[0].Assignment.Target.Member.FieldType == typeof(MonsterModel);
        var appliesMinion = body.Calls.Any(s => s.Call.Method.DeclaringType == typeof(PowerCmd)
            && s.Call.Method.Name == nameof(PowerCmd.Apply) && s.Call.Method.IsGenericMethod
            && s.Call.Method.GetGenericArguments()[0] == typeof(MinionPower));

        foreach (var summonType in poolTypes)
            _summons.Add(summonType);
        if (appliesMinion)
            _powers.Add(typeof(MinionPower));

        var labels = string.Join(", ", poolTypes.Select(ModelLabel));
        var exclusion = excludesLast ? " (excluding the most recently summoned model)" : "";
        var minionText = appliesMinion ? $" with 1 {PowerLabel(typeof(MinionPower))}" : "";
        return new Effect($"summon 1 random enemy from [{labels}]{exclusion}{minionText}");
    }

    private List<Effect>? TryReadSpecialMove(MethodInfo method)
    {
        // TwoTailedRat.CallForBackup: synchronizes CallForBackupCount across all TwoTailedRat enemies and summons TwoTailedRat.
        if (method.Name == "CallForBackup" && method.DeclaringType is { } ratType && typeof(MonsterModel).IsAssignableFrom(ratType))
        {
            var body = Body(method);
            var addsSelfType = body.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CreatureCmd)
                && s.Call.Method.Name == nameof(CreatureCmd.Add) && s.Call.Method.IsGenericMethod
                && s.Call.Method.GetGenericArguments()[0] == ratType);
            if (addsSelfType)
            {
                _summons.Add(ratType);
                return [new Effect($"if an encounter slot is empty: increment shared call for backup count across all {ModelLabel(ratType)} enemies and summon {ModelLabel(ratType)}")];
            }
        }

        // ThievingHopper.ThieveryMove: steals 1 card from target's draw/discard/hand via SwipePower and deals TheftDamage.
        if (method.Name == "ThieveryMove" && method.DeclaringType is { } hopperType && typeof(MonsterModel).IsAssignableFrom(hopperType))
        {
            var body = Body(method);
            var stealsSwipe = body.Calls.Any(s => s.Call.Method.DeclaringType == typeof(SwipePower) && s.Call.Method.Name == "Steal");
            var execStep = body.Calls.FirstOrDefault(s => s.Call.Method.Name == "Execute" && s.Call.Method.DeclaringType?.Name == "AttackCommand");
            if (stealsSwipe && execStep?.Call.Receiver is Call attackCall && Attack(attackCall) is { } attackEffect)
            {
                _powers.Add(typeof(SwipePower));
                return
                [
                    new Effect($"steal 1 card from player target's draw or discard pile (preferring unupgraded non-basic cards) and store it in {PowerLabel(typeof(SwipePower))} on self", AddedPower: typeof(SwipePower)),
                    attackEffect,
                ];
            }
        }

        // DecimillipedeSegment.AfterAddedToRoom only staggers segment visual/initial HP parity and has no post-spawn combat hook.
        if (method.Name == "AfterAddedToRoom" && method.DeclaringType?.Name == "DecimillipedeSegment")
            return [];

        return null;
    }

    private string? DescribeSpawnOverrides(Call addCall, MethodInfo caller)
    {
        if (addCall.Arguments.FirstOrDefault() is not { } spawnedExpr || SummonedMonsterType(spawnedExpr) is null)
            return null;

        var body = Body(caller);
        var overrides = new List<string>();
        foreach (var step in body.Calls)
        {
            if (step.Call.Receiver != spawnedExpr
                || step.Call.Method is not MethodInfo { Name: var setterName } setter
                || !setter.IsSpecialName || !setterName.StartsWith("set_", StringComparison.Ordinal)
                || step.Call.Arguments is not [var rhs])
            {
                continue;
            }

            var propName = setterName[4..];
            if (setter.DeclaringType is { } owner && BackingField(owner, setter) is { } backing && !IsGameplayField(backing))
                continue;
            if (propName.Contains("Anim", StringComparison.Ordinal) || propName.Contains("Vfx", StringComparison.Ordinal))
                continue;
            if (ValueText(rhs) is { } rhsText)
                overrides.Add($"{HumanizeLower(propName)}={rhsText}");
        }

        return overrides.Count > 0 ? $" (with {string.Join(", ", overrides)})" : null;
    }

    private string? CorridorCombatCondition(Test test)
    {
        var expected = test.Expected;
        var value = test.Value;

        if (value is Call { Method.Name: nameof(string.IsNullOrEmpty), Arguments: [var slotArg] }
            && (slotArg is Call { Method.Name: "GetNextSlot" } || slotArg is Call freeCall && IsFreeSlotSearch(freeCall)))
        {
            return expected ? "no encounter slot is empty" : "an encounter slot is empty";
        }

        if (value is Call { Method: MethodInfo { Name: "CanSummon" } canSummonMethod, Receiver: Input { Name: "this" }, Arguments.Length: 0 }
            && canSummonMethod.DeclaringType == monster?.GetType()
            && DescribeTwoTailedRatCanSummon(canSummonMethod, expected) is { } summonRule)
        {
            return summonRule;
        }

        // Expand computed boolean properties on the current monster that have no backing field (e.g., CanFabricate, CanLay).
        if (value is Call { Method: MethodInfo getter, Receiver: Input { Name: "this" }, Arguments.Length: 0 }
            && getter.ReturnType == typeof(bool) && getter.Name.StartsWith("get_", StringComparison.Ordinal)
            && monster is not null && getter.DeclaringType?.IsInstanceOfType(monster) == true
            && BackingField(getter.DeclaringType, getter) is null
            && Body(getter) is { Incomplete: false, Returns: [{ Conditions.Length: 0, Value: var computed }] }
            && Condition(new Test(computed, expected)) is { Length: > 0 } expanded && expanded != Never)
        {
            return expanded;
        }

        if (value is Call { Method.Name: "get_Count", Receiver: Call { Method.Name: "get_HittableEnemies" } })
            return expected ? "at least one hittable enemy exists" : "no hittable enemy exists";

        if (value is Operation { Operator: "ble" or "bgt" or "clt" or "cgt",
            Left: Call { Method.Name: "get_Count", Receiver: Call { Method.Name: "get_PlayersTakingExtraTurn" } },
            Right: Constant { Data: 0 } } extraOp)
        {
            var isZero = (extraOp.Operator == "ble") == expected;
            return isZero ? "not an extra player turn" : "during an extra player turn";
        }

        if (value is Input { Name: "fromHandDraw" })
            return expected ? "draw is turn-start hand draw" : "draw is not turn-start hand draw";

        if (value is Input { Name: "causedByEthereal" })
            return expected ? "exhaust was caused by Ethereal" : "exhaust was not caused by Ethereal";

        return null;
    }

    private string? DescribeTwoTailedRatCanSummon(MethodInfo method, bool expected)
    {
        var body = Body(method);
        if (body.Incomplete)
            return null;
        var checksTurns = body.Returns.Any(r => r.Conditions.Any(c => c.Value is Operation { Left: Call { Method.Name: "get_TurnsUntilSummonable" } }));
        var limitCap = body.Returns
            .SelectMany(r => r.Conditions)
            .Select(c => c.Value is Operation { Left: Call { Method.Name: "get_CallForBackupCount" }, Right: Constant { Data: int cap } } && cap > 0 ? (int?)cap : null)
            .FirstOrDefault(cap => cap is not null);
        if (!checksTurns || limitCap is not { } maxBackups)
            return null;

        return expected
            ? $"turns until summonable≤0 and call for backup count<{maxBackups} and an encounter slot is empty and no ally already intends CALL_FOR_BACKUP_MOVE"
            : $"turns until summonable>0 or call for backup count≥{maxBackups} or no encounter slot is empty or an ally already intends CALL_FOR_BACKUP_MOVE";
    }

    private string? CorridorCombatValue(Value value)
    {
        // Teammate counts on the current monster (e.g., Fabricator.CanFabricate, Ovicopter.CanLay, LivingShield.GetAllyCount).
        if (value is Call { Method.Name: nameof(Enumerable.Count), Arguments: [Call { Method.Name: "GetTeammatesOf", Arguments: [var focus] },
                Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo pred }] }] }
            && IsOwner(focus) && TeammateCountSubject(pred) is { } countLabel)
        {
            return countLabel;
        }

        // Unfiltered GetTeammatesOf(Creature) used as target list (e.g., TheObscura.WailMove).
        if (value is Call { Method.Name: "GetTeammatesOf", Arguments: [var selfFocus] } && IsOwner(selfFocus))
            return "all enemies on self's side";

        // Random hittable enemy target (e.g., JuggernautPower).
        if (value is Call { Method.Name: "NextItem", Arguments: [Call { Method.Name: "get_HittableEnemies" }] })
            return "a random hittable enemy";

        if (value is Call { Method.Name: "get_HittableEnemies" })
            return "all hittable enemies";

        if (value is Call { Method.DeclaringType: var mathType, Method.Name: "Max" or "Min", Arguments: [var mathLeft, var mathRight] } mathCall
            && mathType == typeof(Math) && ValueText(mathLeft) is { } leftText && ValueText(mathRight) is { } rightText)
        {
            return $"{mathCall.Method.Name.ToLowerInvariant()}({leftText}, {rightText})";
        }

        return null;
    }

    private string? TeammateCountSubject(MethodInfo predicate)
    {
        var body = Body(predicate);
        if (body.Incomplete || body.Returns.Count == 0)
            return null;

        // c => c.IsAlive (includes self in GetTeammatesOf)
        if (body.Returns is [{ Conditions.Length: 0, Value: Call { Method.Name: "get_IsAlive", Receiver: Input } }])
            return "living enemies on self's side";

        // c => c.IsAlive && c != Creature (excludes self)
        var excludesSelf = body.Returns.Any(r => r.Value is Operation { Operator: "ceq", Right: Constant { Data: 0 },
            Left: Operation { Operator: "ceq", Left: Input, Right: var selfRef } } && IsOwner(selfRef));
        var checksAlive = body.Returns.Any(r => r.Conditions.Any(c => c is { Expected: true, Value: Call { Method.Name: "get_IsAlive" } }));
        if (excludesSelf && checksAlive)
            return "other living allies";

        return null;
    }

    private void ReadCorridorPowerHooks(Type type, string prefix, HashSet<string> handled, List<string> lines)
    {
        if (ReadIllusionPowerHooks(type, prefix, out var illusionLines))
        {
            handled.Add("AfterApplied");
            handled.Add("AfterDeath");
            handled.Add("ShouldAllowHitting");
            handled.Add("ShouldCreatureBeRemovedFromCombatAfterDeath");
            handled.Add("ShouldPowerBeRemovedOnDeath");
            lines.AddRange(illusionLines);
        }
        if (ReadBurrowedPowerHooks(type, prefix, out var burrowedLines))
        {
            handled.Add("ShouldClearBlock");
            handled.Add("AfterBlockBroken");
            handled.Add("AfterRemoved");
            lines.AddRange(burrowedLines);
        }
        if (ReadFlutterPowerHooks(type, prefix, out var flutterLines))
        {
            handled.Add("ModifyDamageMultiplicative");
            handled.Add("AfterDamageReceived");
            lines.AddRange(flutterLines);
        }
        if (ReadSuckPowerHook(type, prefix, out var suckLine))
        {
            handled.Add("AfterAttack");
            lines.Add(suckLine);
        }
        if (ReadTenderPowerHooks(type, prefix, out var tenderLines))
        {
            handled.Add("AfterCardPlayed");
            handled.Add("AfterSideTurnEnd");
            lines.AddRange(tenderLines);
        }
        if (ReadPossessStatHooks(type, prefix, out var possessLines))
        {
            handled.Add("AfterPowerAmountChanged");
            handled.Add("AfterDeath");
            lines.AddRange(possessLines);
        }
        if (ReadSwipePowerHooks(type, prefix, out var swipeLine))
        {
            handled.Add("BeforeDeath");
            lines.Add(swipeLine);
        }
        if (ReadThieveryPowerHooks(type, prefix, out var thieveryLine))
        {
            lines.Add(thieveryLine);
        }
        if (ReadHeistPowerHooks(type, prefix, out var heistLine))
        {
            handled.Add("BeforeDeath");
            lines.Add(heistLine);
        }
        if (ReadSurprisePowerHooks(type, prefix, out var surpriseLine))
        {
            handled.Add("AfterDeath");
            lines.Add(surpriseLine);
        }
        if (ReadReattachPowerHooks(type, prefix, out var reattachLines))
        {
            handled.Add("AfterDeath");
            handled.Add("ShouldAllowHitting");
            handled.Add("ShouldCreatureBeRemovedFromCombatAfterDeath");
            handled.Add("ShouldPowerBeRemovedAfterOwnerDeath");
            handled.Add("ShouldOwnerDeathTriggerFatal");
            lines.AddRange(reattachLines);
        }
    }

    private bool ReadReattachPowerHooks(Type power, string prefix, out List<string> lines)
    {
        lines = [];
        if (FindOverride(power, "AfterDeath") is not { } death
            || FindOverride(power, "ShouldAllowHitting") is not { } allowHit
            || FindOverride(power, "ShouldCreatureBeRemovedFromCombatAfterDeath") is not { } keepBody
            || FindOverride(power, "ShouldOwnerDeathTriggerFatal") is not { } fatal
            || power.GetMethod("DoReattach", Declared) is not { } reattach
            || Body(death) is not { Incomplete: false } deathBody
            || Body(allowHit) is not { Incomplete: false }
            || Body(keepBody) is not { Incomplete: false }
            || Body(fatal) is not { Incomplete: false }
            || Body(reattach) is not { Incomplete: false } reattachBody)
            return false;

        var setsMove = deathBody.Calls.Any(s => s.Call.Method.Name == "SetMoveImmediate");
        var healsStacks = reattachBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.Heal));
        if (!setsMove || !healsStacks)
            return false;

        lines.Add($"{prefix}On owner death: if death was not prevented: owner remains in combat and becomes untargetable while reviving; if another teammate with {PowerLabel(power)} is still alive, immediately forces next move to reattach and heal stacks HP (combat ends and Fatal triggers only when all segments with {PowerLabel(power)} are dead simultaneously)");
        return true;
    }

    private bool ReadIllusionPowerHooks(Type power, string prefix, out List<string> lines)
    {
        lines = [];
        if (FindOverride(power, "AfterDeath") is not { } death
            || FindOverride(power, "ShouldAllowHitting") is not { } allowHit
            || FindOverride(power, "ShouldCreatureBeRemovedFromCombatAfterDeath") is not { } keepBody
            || power.GetMethod("ReviveMove", Declared) is not { } revive
            || Body(death) is not { Incomplete: false } deathBody
            || Body(allowHit) is not { Incomplete: false }
            || Body(keepBody) is not { Incomplete: false }
            || Body(revive) is not { Incomplete: false } reviveBody)
            return false;

        var setsRevive = deathBody.Calls.Any(s => s.Call.Method.Name == "SetMoveImmediate");
        var healsFull = reviveBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.Heal));
        if (!setsRevive || !healsFull)
            return false;

        _powers.Add(typeof(MinionPower));
        lines.Add($"{prefix}After this power is applied: if owner lacks {PowerLabel(typeof(MinionPower))}: attempt to apply 1 {PowerLabel(typeof(MinionPower))} to owner");
        lines.Add($"{prefix}On owner death: if death was not prevented: owner remains in combat, becomes untargetable while reviving, removes buffs and temporary debuffs (keeping permanent debuffs), and replaces its next move with reviving to full HP");
        return true;
    }

    private bool ReadBurrowedPowerHooks(Type power, string prefix, out List<string> lines)
    {
        lines = [];
        if (FindOverride(power, "ShouldClearBlock") is not { } clearBlock
            || FindOverride(power, "AfterBlockBroken") is not { } broken
            || FindOverride(power, "AfterRemoved") is not { } removed
            || Body(clearBlock) is not { Incomplete: false }
            || Body(broken) is not { Incomplete: false } brokenBody
            || Body(removed) is not { Incomplete: false } removedBody)
            return false;

        var stunsOnBreak = brokenBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.Stun));
        var removesSelf = brokenBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(PowerCmd) && s.Call.Method.Name == nameof(PowerCmd.Remove));
        var losesAllBlock = removedBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.LoseBlock));
        if (!stunsOnBreak || !removesSelf || !losesAllBlock)
            return false;

        var followUpClause = "";
        var stunStep = brokenBody.Calls.First(s => s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.Stun));
        if (stunStep.Call.Arguments.LastOrDefault() is Constant { Data: string nextMoveId })
        {
            if (monster is not null && ShowableMove(nextMoveId))
                _forcedMoves.Add(nextMoveId);
            followUpClause = $", then → {nextMoveId}";
        }

        lines.Add($"{prefix}Block expires at turn start: if owner=event target: no");
        lines.Add($"{prefix}After block is broken: if event target=owner: stun owner for one action{followUpClause} and remove {PowerLabel(power)}");
        lines.Add($"{prefix}After this power is removed: remove all Block from former owner");
        return true;
    }

    private bool ReadFlutterPowerHooks(Type power, string prefix, out List<string> lines)
    {
        lines = [];
        if (FindOverride(power, "ModifyDamageMultiplicative") is not { } mult
            || FindOverride(power, "AfterDamageReceived") is not { } afterDmg
            || Body(mult) is not { Incomplete: false } multBody
            || Body(afterDmg) is not { Incomplete: false } dmgBody
            || ModelDb.GetByIdOrNull<PowerModel>(ModelDb.GetId(power)) is not { } canonical
            || !canonical.DynamicVars.TryGetValue("DamageDecrease", out var decreaseVar))
            return false;

        var decrementsOnUnblocked = dmgBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(PowerCmd) && s.Call.Method.Name == nameof(PowerCmd.Decrement));
        var stunsAtZero = dmgBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.Stun));
        if (!decrementsOnUnblocked || !stunsAtZero || multBody.Returns.Count == 0)
            return false;

        var factor = decreaseVar.BaseValue / 100m;
        lines.Add($"{prefix}Damage multiplier per hit, after additions: if target=owner and powered attack: ×{Number(factor)}");
        lines.Add($"{prefix}After damage resolves: if target=owner and powered attack and nonzero HP lost: subtract 1 {PowerLabel(power)} stack; if stacks reach 0, stun owner for one action");
        return true;
    }

    private bool ReadSuckPowerHook(Type power, string prefix, out string line)
    {
        line = "";
        if (FindOverride(power, "AfterAttack") is not { } after || Body(after) is not { Incomplete: false } body)
            return false;

        var appliesStr = body.Calls.Any(s => s.Call.Method.DeclaringType == typeof(PowerCmd)
            && s.Call.Method.Name == nameof(PowerCmd.Apply) && s.Call.Method.IsGenericMethod
            && s.Call.Method.GetGenericArguments()[0] == typeof(StrengthPower));
        var checksPetDedup = body.FieldSteps.Any(f => f.Assignment.Target.Member.Name == "petHit");
        if (!appliesStr || !checksPetDedup)
            return false;

        _powers.Add(typeof(StrengthPower));
        line = $"{prefix}Once after the entire attack resolves: if attacker=owner and attack is a powered attack and deals nonzero unblocked damage to an opponent (counting a player and their pet once total): attempt to apply stacks {PowerLabel(typeof(StrengthPower))} to owner";
        return true;
    }

    private bool ReadTenderPowerHooks(Type power, string prefix, out List<string> lines)
    {
        lines = [];
        if (FindOverride(power, "AfterCardPlayed") is not { } played
            || FindOverride(power, "AfterSideTurnEnd") is not { } turnEnd
            || Body(played) is not { Incomplete: false } playedBody
            || Body(turnEnd) is not { Incomplete: false } endBody)
            return false;

        var losesStr = playedBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(PowerCmd)
            && s.Call.Method.Name == nameof(PowerCmd.Apply) && s.Call.Method.IsGenericMethod
            && s.Call.Method.GetGenericArguments()[0] == typeof(StrengthPower)
            && Numeric(Argument(s.Call, "amount")) == -1m);
        var losesDex = playedBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(PowerCmd)
            && s.Call.Method.Name == nameof(PowerCmd.Apply) && s.Call.Method.IsGenericMethod
            && s.Call.Method.GetGenericArguments()[0] == typeof(DexterityPower)
            && Numeric(Argument(s.Call, "amount")) == -1m);
        var restoresAtTurnEnd = endBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(PowerCmd)
            && s.Call.Method.Name == nameof(PowerCmd.Apply) && s.Call.Method.IsGenericMethod
            && s.Call.Method.GetGenericArguments()[0] == typeof(StrengthPower));
        if (!losesStr || !losesDex || !restoresAtTurnEnd)
            return false;

        lines.Add($"{prefix}After each card play resolves: if played card's player=owner: apply −1 {PowerLabel(typeof(StrengthPower))} and −1 {PowerLabel(typeof(DexterityPower))} to owner for this turn");
        lines.Add($"{prefix}After side turn ends: if owner participates this turn: restore the {PowerLabel(typeof(StrengthPower))} and {PowerLabel(typeof(DexterityPower))} lost from this power this turn");
        return true;
    }

    private bool ReadPossessStatHooks(Type power, string prefix, out List<string> lines)
    {
        lines = [];
        if (FindOverride(power, "AfterPowerAmountChanged") is not { } changed
            || FindOverride(power, "AfterDeath") is not { } death
            || Body(changed) is not { Incomplete: false }
            || Body(death) is not { Incomplete: false } deathBody)
            return false;

        var applyBack = deathBody.Calls.FirstOrDefault(s => s.Call.Method.DeclaringType == typeof(PowerCmd)
            && s.Call.Method.Name == nameof(PowerCmd.Apply) && s.Call.Method.IsGenericMethod);
        if (applyBack is null)
            return false;
        var usesDictionary = deathBody.Calls.Any(s => s.Call.Method.Name == "get_Key" || s.Call.Method.Name == "get_Value");
        if (!usesDictionary)
            return false;

        var statType = applyBack.Call.Method.GetGenericArguments()[0];
        _powers.Add(statType);
        lines.Add($"{prefix}Stat theft tracking: records negative {PowerLabel(statType)} applied by owner to each player; on owner death (if death was not prevented), restores the recorded {PowerLabel(statType)} to each player");
        return true;
    }

    private bool ReadSwipePowerHooks(Type power, string prefix, out string line)
    {
        line = "";
        if (power != typeof(SwipePower) || FindOverride(power, "BeforeDeath") is not { } beforeDeath
            || Body(beforeDeath) is not { Incomplete: false } body)
            return false;

        var returnsToDeck = body.Calls.Any(s => s.Call.Method.Name == "AddCard")
            && body.Calls.Any(s => s.Call.Method.Name == nameof(CombatRoom.AddExtraReward));
        if (!returnsToDeck)
            return false;

        line = $"{prefix}Before death: if event target=owner and a stolen card is stored: return that card to its player's deck and combat rewards";
        return true;
    }

    private bool ReadThieveryPowerHooks(Type power, string prefix, out string line)
    {
        line = "";
        if (power != typeof(ThieveryPower) || power.GetMethod("Steal", Declared) is not { } steal
            || Body(steal) is not { Incomplete: false } body)
            return false;

        var losesGold = body.Calls.Any(s => s.Call.Method.DeclaringType == typeof(PlayerCmd) && s.Call.Method.Name == nameof(PlayerCmd.LoseGold));
        if (!losesGold)
            return false;

        _powers.Add(typeof(HeistPower));
        line = $"{prefix}When triggered by owner's move: steals min(stacks, target player's Gold) Gold and stores it as {PowerLabel(typeof(HeistPower))} on owner";
        return true;
    }

    private bool ReadHeistPowerHooks(Type power, string prefix, out string line)
    {
        line = "";
        if (power != typeof(HeistPower) || FindOverride(power, "BeforeDeath") is not { } beforeDeath
            || Body(beforeDeath) is not { Incomplete: false } body)
            return false;

        var addsGoldReward = body.Calls.Any(s => s.Call.Method.Name == nameof(CombatRoom.AddExtraReward));
        if (!addsGoldReward)
            return false;

        line = $"{prefix}Before death: if event target=owner: add stacks Gold to combat rewards";
        return true;
    }

    private bool ReadSurprisePowerHooks(Type power, string prefix, out string line)
    {
        line = "";
        if (FindOverride(power, "AfterDeath") is not { } afterDeath
            || Body(afterDeath) is not { Incomplete: false } body)
            return false;

        var firstSpawn = body.Calls
            .Select(s => s.Call.Method is { Name: "Monster", IsGenericMethod: true } m && m.DeclaringType == typeof(ModelDb)
                ? m.GetGenericArguments()[0]
                : null)
            .FirstOrDefault(t => t is not null);
        var secondSpawn = body.Calls
            .Select(s => s.Call.Method is { Name: nameof(CreatureCmd.Add), IsGenericMethod: true } m && m.DeclaringType == typeof(CreatureCmd)
                ? m.GetGenericArguments()[0]
                : null)
            .FirstOrDefault(t => t is not null);
        var transfersHeist = body.Calls.Any(s => s.Call.Method is { Name: "Power", IsGenericMethod: true } m
            && m.DeclaringType == typeof(ModelDb) && m.GetGenericArguments()[0] == typeof(HeistPower));
        if (firstSpawn is null || secondSpawn is null || !transfersHeist)
            return false;

        _summons.Add(firstSpawn);
        _summons.Add(secondSpawn);
        _powers.Add(typeof(HeistPower));
        line = $"{prefix}On death event: if death was not prevented and owner=target: summon {ModelLabel(firstSpawn)} (transferring stolen Gold from owner's {PowerLabel(typeof(ThieveryPower))} into {PowerLabel(typeof(HeistPower))} on {ModelLabel(firstSpawn)}) and summon {ModelLabel(secondSpawn)}";
        return true;
    }
}
