namespace Sts2Mod.Core.Observation;

/// <summary>
/// Isolates hypothetical native upgrade preview rendering failures from live card observations.
/// </summary>
public sealed class UpgradePreviewReader(Action<Exception> reportFailure)
{
    private bool _failureReported;

    public UpgradePreview Read(bool isUpgradable, Func<CardDefinitionPreview> render)
    {
        if (!isUpgradable)
            return new NotUpgradablePreview();

        try
        {
            var preview = render();
            _failureReported = false;
            return new AvailableUpgradePreview(preview);
        }
        catch (Exception error)
        {
            if (!_failureReported)
            {
                _failureReported = true;
                reportFailure(error);
            }
            return new UnavailableUpgradePreview();
        }
    }
}
