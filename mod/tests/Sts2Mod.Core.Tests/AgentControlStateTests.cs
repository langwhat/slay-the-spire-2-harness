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
    public void WriteJson_EnforcesControlContract()
    {
        var control = new AgentControlState();

        var writeReply = control.WriteJson("{\"mode\":\"running\"}");
        Assert.Equal(200, writeReply.StatusCode);
        Assert.Equal("{\"mode\":\"running\"}", writeReply.Json);
        Assert.True(control.IsRunning);

        var pauseReply = control.WriteJson("{\"mode\":\"paused\"}");
        Assert.Equal(200, pauseReply.StatusCode);
        Assert.Equal("{\"mode\":\"paused\"}", pauseReply.Json);
        Assert.False(control.IsRunning);

        foreach (var invalid in new[]
                 {
                     "{}",
                     "{\"mode\":\"fast\"}",
                     "{\"mode\":\"paused\",\"mode\":\"running\"}",
                     "{\"mode\":\"paused\",\"extra\":1}",
                 })
        {
            var reply = control.WriteJson(invalid);
            Assert.Equal(400, reply.StatusCode);
            using var doc = JsonDocument.Parse(reply.Json);
            Assert.Equal("invalid_request", doc.RootElement.GetProperty("error").GetProperty("code").GetString());
        }
    }
}
