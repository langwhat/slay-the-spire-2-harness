using System.Text.Json;
using Sts2Mod.Core.Control;
using Xunit;

namespace Sts2Mod.Core.Tests;

public sealed class AgentControlStateTests
{
    [Fact]
    public void ToggleAndSetMode_UpdateIntentAndNotifyOnPause()
    {
        var control = new AgentControlState();
        var pauseEvents = 0;
        control.Paused += () => pauseEvents++;

        Assert.Equal(AgentControlMode.Paused, control.Mode);
        Assert.False(control.IsRunning);

        Assert.Equal(AgentControlMode.Running, control.Toggle());
        Assert.True(control.IsRunning);
        Assert.Equal(0, pauseEvents);

        Assert.Equal(AgentControlMode.Paused, control.Toggle());
        Assert.False(control.IsRunning);
        Assert.Equal(1, pauseEvents);

        control.SetMode(AgentControlMode.Running);
        control.SetMode(AgentControlMode.Paused);
        Assert.Equal(2, pauseEvents);
    }

    [Fact]
    public void RunBoundaryRules_ResetOrdinaryRunsToPausedAndPreserveArmedCheckpointLoadsOnce()
    {
        var control = new AgentControlState();
        control.SetMode(AgentControlMode.Running);

        Assert.False(control.ApplyRunStartedBoundary());
        Assert.Equal(AgentControlMode.Paused, control.Mode);

        control.SetMode(AgentControlMode.Running);
        control.ArmRunLoadBoundary();
        Assert.True(control.ApplyRunStartedBoundary());
        Assert.Equal(AgentControlMode.Running, control.Mode);

        Assert.False(control.ApplyRunStartedBoundary());
        Assert.Equal(AgentControlMode.Paused, control.Mode);

        control.SetMode(AgentControlMode.Running);
        control.ArmRunLoadBoundary();
        control.CancelRunLoadBoundary();
        Assert.False(control.ApplyRunStartedBoundary());
        Assert.Equal(AgentControlMode.Paused, control.Mode);
    }

    [Fact]
    public void ReadAndWriteJson_EnforceControlContract()
    {
        var control = new AgentControlState();

        var (readCode, readJson) = control.ReadJson();
        Assert.Equal(200, readCode);
        Assert.Equal("{\"mode\":\"paused\"}", readJson);

        var (writeCode, writeJson) = control.WriteJson("{\"mode\":\"running\"}");
        Assert.Equal(200, writeCode);
        Assert.Equal("{\"mode\":\"running\"}", writeJson);
        Assert.True(control.IsRunning);

        foreach (var invalid in new[]
                 {
                     "{}",
                     "{\"mode\":\"fast\"}",
                     "{\"mode\":\"paused\",\"mode\":\"running\"}",
                     "{\"mode\":\"paused\",\"extra\":1}",
                 })
        {
            var (code, json) = control.WriteJson(invalid);
            Assert.Equal(400, code);
            using var doc = JsonDocument.Parse(json);
            Assert.Equal("invalid_request", doc.RootElement.GetProperty("error").GetProperty("code").GetString());
        }
    }
}
