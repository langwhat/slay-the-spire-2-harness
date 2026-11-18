using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using Sts2Mod.Core.Transport;
using Xunit;

namespace Sts2Mod.Core.Tests;

public sealed class LoopbackHttpServerTests
{
    private sealed class FakeHandlers : IModHttpHandlers
    {
        public Func<(int StatusCode, string Json)> OnStatus { get; set; } = static () => (200, "{\"route\":\"status\"}");
        public Func<string, Task<(int StatusCode, string Json)>> OnInspect { get; set; } =
            static body => Task.FromResult((200, $"{{\"route\":\"inspect\",\"body\":{body}}}"));

        public (int StatusCode, string Json) GetStatus() => OnStatus();
        public (int StatusCode, string Json) GetState() => (200, "{\"route\":\"state\"}");
        public Task<(int StatusCode, string Json)> Inspect(string body) => OnInspect(body);
        public Task<(int StatusCode, string Json)> Catalog(string body) => Task.FromResult((200, "{\"route\":\"catalog\"}"));
        public (int StatusCode, string Json) SubmitAction(string body) => (200, "{\"route\":\"action\"}");
        public (int StatusCode, string Json) GetControl() => (200, "{\"route\":\"get_control\"}");
        public (int StatusCode, string Json) SetControl(string body) => (200, "{\"route\":\"set_control\"}");
        public Task<(int StatusCode, string Json)> PresentMap(string body) => Task.FromResult((200, "{\"route\":\"present\"}"));
        public Task<(int StatusCode, string Json)> VerifyMapRestoration(string body) => Task.FromResult((200, "{\"route\":\"verify\"}"));
        public (int StatusCode, string Json) GetCheckpointIdentity() => (200, "{\"route\":\"checkpoint_identity\"}");
        public (int StatusCode, string Json) SubmitCheckpoint(string body) => (200, "{\"route\":\"checkpoint\"}");
        public Task<(int StatusCode, string Json)> PrepareLaboratoryScenario(string body) => Task.FromResult((200, "{\"route\":\"scenario\"}"));
    }

    private static int AllocateLoopbackPort()
    {
        using var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        return ((IPEndPoint)listener.LocalEndpoint).Port;
    }

    [Fact]
    public async Task AllSupportedRoutes_DispatchAndReturnProtocolJson()
    {
        var handlers = new FakeHandlers();
        var port = AllocateLoopbackPort();
        using var server = new LoopbackHttpServer(port, handlers, maxBodyBytes: 256, maxConcurrentRequests: 4);
        server.Start();

        using var client = new HttpClient { BaseAddress = new Uri(server.Prefix) };

        Assert.Equal("{\"route\":\"status\"}", await client.GetStringAsync("/status"));
        Assert.Equal("{\"route\":\"state\"}", await client.GetStringAsync("/state"));
        Assert.Equal("{\"route\":\"get_control\"}", await client.GetStringAsync("/control"));
        Assert.Equal("{\"route\":\"checkpoint_identity\"}", await client.GetStringAsync("/operator/checkpoint/identity"));

        using var emptyInspect = await client.PostAsync("/inspect", new StringContent("", Encoding.UTF8, "application/json"));
        Assert.Equal(HttpStatusCode.OK, emptyInspect.StatusCode);
        Assert.Equal("{\"route\":\"inspect\",\"body\":{}}", await emptyInspect.Content.ReadAsStringAsync());

        foreach (var (path, expected) in new[]
                 {
                     ("/catalog", "catalog"),
                     ("/action", "action"),
                     ("/control", "set_control"),
                     ("/map/present", "present"),
                     ("/map/restore/verify", "verify"),
                     ("/operator/checkpoint", "checkpoint"),
                     ("/operator/eval/scenario", "scenario"),
                 })
        {
            using var response = await client.PostAsync(path, new StringContent("{}", Encoding.UTF8, "application/json"));
            Assert.Equal(HttpStatusCode.OK, response.StatusCode);
            using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
            Assert.Equal(expected, doc.RootElement.GetProperty("route").GetString());
        }
    }

    [Fact]
    public async Task UnknownRoutesQueryStringsAndOversizedBodies_ReturnHttp400InvalidRequest()
    {
        var handlers = new FakeHandlers();
        var port = AllocateLoopbackPort();
        using var server = new LoopbackHttpServer(port, handlers, maxBodyBytes: 32, maxConcurrentRequests: 4);
        server.Start();

        using var client = new HttpClient { BaseAddress = new Uri(server.Prefix) };

        using var unknown = await client.GetAsync("/act");
        Assert.Equal(HttpStatusCode.BadRequest, unknown.StatusCode);
        using var unknownDoc = JsonDocument.Parse(await unknown.Content.ReadAsStringAsync());
        Assert.Equal("invalid_request", unknownDoc.RootElement.GetProperty("error").GetProperty("code").GetString());

        using var withQuery = await client.GetAsync("/status?extra=1");
        Assert.Equal(HttpStatusCode.BadRequest, withQuery.StatusCode);

        var largePayload = new string('x', 64);
        using var oversized = await client.PostAsync("/action", new StringContent(largePayload, Encoding.UTF8, "application/json"));
        Assert.Equal(HttpStatusCode.BadRequest, oversized.StatusCode);
        using var oversizedDoc = JsonDocument.Parse(await oversized.Content.ReadAsStringAsync());
        Assert.Equal("body", oversizedDoc.RootElement.GetProperty("error").GetProperty("fields")[0].GetString());
    }

    [Fact]
    public async Task HandlerExceptionsAndConcurrencyOverflow_ReturnHttp500TechnicalError()
    {
        var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var handlers = new FakeHandlers
        {
            OnStatus = static () => throw new InvalidOperationException("Exploded"),
            OnInspect = async _ =>
            {
                entered.TrySetResult();
                await release.Task;
                return (200, "{\"ok\":true}");
            },
        };

        var port = AllocateLoopbackPort();
        using var server = new LoopbackHttpServer(port, handlers, maxBodyBytes: 256, maxConcurrentRequests: 1);
        server.Start();

        using var client = new HttpClient { BaseAddress = new Uri(server.Prefix) };

        using var failed = await client.GetAsync("/status");
        Assert.Equal(HttpStatusCode.InternalServerError, failed.StatusCode);
        using var failedDoc = JsonDocument.Parse(await failed.Content.ReadAsStringAsync());
        Assert.Equal("internal_error", failedDoc.RootElement.GetProperty("error").GetProperty("code").GetString());

        var slowCall = client.PostAsync("/inspect", new StringContent("{}", Encoding.UTF8, "application/json"));
        await entered.Task;

        using var overflow = await client.GetAsync("/state");
        Assert.Equal(HttpStatusCode.InternalServerError, overflow.StatusCode);

        release.TrySetResult();
        using var slowResponse = await slowCall;
        Assert.Equal(HttpStatusCode.OK, slowResponse.StatusCode);
    }
}
