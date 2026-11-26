using Godot;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using Sts2Mod.Core.Execution;

namespace Sts2Mod.Observation;

internal static class NodeHierarchy
{
    public static IEnumerable<Node> Descendants(Node node)
    {
        foreach (var child in node.GetChildren())
        {
            yield return child;
            foreach (var nested in Descendants(child))
                yield return nested;
        }
    }

    public static IEnumerable<Node> Ancestors(Node node)
    {
        for (var parent = node.GetParent(); parent is not null; parent = parent.GetParent())
            yield return parent;
    }

    public static NClickableControl? FindEnchantCloseButton(NDeckEnchantSelectScreen screen) =>
        screen.GetNodeOrNull<NClickableControl>("%Close") is { IsEnabled: true } button
        && !button.IsQueuedForDeletion()
        && button.IsVisibleInTree()
            ? button
            : null;

    public static NClickableControl? FindRewardDeclineButton(NCardRewardSelectionScreen screen)
    {
        var alternatives = RunSurfaceLifecycle.GetCardRewardAlternatives(screen);
        var buttons = screen.GetNode<Control>("UI/RewardAlternatives").GetChildren()
            .OfType<NCardRewardAlternativeButton>()
            .Where(button => !button.IsQueuedForDeletion())
            .ToArray();
        var count = Math.Min(alternatives.Count, buttons.Length);
        if (count == 0)
            return null;
        var candidates = new CardRewardAlternativeAvailability[count];
        for (var index = 0; index < count; index++)
        {
            var button = buttons[index];
            candidates[index] = new CardRewardAlternativeAvailability(
                alternatives[index].OptionId,
                GodotObject.IsInstanceValid(button) && button.IsVisibleInTree() && button.IsEnabled);
        }
        var selected = StepRules.SelectCardRewardDeclineIndex(candidates);
        return selected >= 0 ? buttons[selected] : null;
    }
}
