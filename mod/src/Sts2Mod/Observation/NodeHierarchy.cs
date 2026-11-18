using Godot;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;

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

    public static NClickableControl? FindRewardSkipButton(NCardRewardSelectionScreen screen)
    {
        var alternatives = RunSurfaceLifecycle.GetCardRewardAlternatives(screen);
        var buttons = screen.GetNode<Control>("UI/RewardAlternatives").GetChildren()
            .OfType<NCardRewardAlternativeButton>()
            .Where(button => !button.IsQueuedForDeletion())
            .ToArray();
        for (var index = 0; index < Math.Min(alternatives.Count, buttons.Length); index++)
        {
            if (string.Equals(alternatives[index].OptionId, "SKIP", StringComparison.OrdinalIgnoreCase))
                return buttons[index];
        }
        return null;
    }
}
