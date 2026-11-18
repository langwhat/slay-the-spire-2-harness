using System.Collections.Immutable;
using Sts2Mod.Core.Observation;

namespace Sts2Mod.Core.Execution;

public sealed record StepTargetBinding(string? Ref, InvalidStepTarget? Error);

public static class StepRules
{
    public static StepTargetBinding BindTarget(
        string? requested,
        bool required,
        ImmutableArray<string> legalTargets)
    {
        if (!required)
            return new(null, null);

        if (requested is not null)
        {
            return legalTargets.Contains(requested, StringComparer.Ordinal)
                ? new(requested, null)
                : new(null, new InvalidStepTarget("Choose a current legal native target.", requested, legalTargets));
        }

        return legalTargets.Length == 1
            ? new(legalTargets[0], null)
            : new(null, new InvalidStepTarget("Choose a current legal native target.", null, legalTargets));
    }

    public static StepCause? ValidateSelection(ImmutableArray<string> requested, SelectionView selection)
    {
        var invalid = requested
            .Where(reference => !selection.Candidates.Contains(reference, StringComparer.Ordinal))
            .ToImmutableArray();
        if (!invalid.IsEmpty)
            return new InvalidStepRefs("The references are not current members of this native input.", invalid);

        return requested.Length < selection.Min || requested.Length > selection.Max
            ? new InvalidStepSelection(
                "Submit the complete selection within the native limits.",
                selection.Min,
                selection.Max)
            : null;
    }
}
