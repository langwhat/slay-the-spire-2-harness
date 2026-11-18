using System.IO;
using System.Net;
using System.Text;

namespace Sts2Mod.Core.Transport;

/// <summary>
/// Unified HTTP surface exposed by the Mod on loopback.
/// </summary>
public interface IModHttpHandlers
{
    HttpReply GetStatus();
    HttpReply GetState();
    Task<HttpReply> Inspect(string body);
    Task<HttpReply> Catalog(string body);
    HttpReply SubmitAction(string body);
    HttpReply SetControl(string body);
    Task<HttpReply> PresentMap(string body);
    Task<HttpReply> VerifyMapRestoration(string body);
    HttpReply GetCheckpointIdentity();
    HttpReply SubmitCheckpoint(string body);
    Task<HttpReply> PrepareLaboratoryScenario(string body);
}

/// <summary>
/// Loopback-only HTTP server returning strictly <c>200</c>, <c>400</c>, or <c>500</c> protocol JSON responses.
/// </summary>
public sealed class LoopbackHttpServer : IDisposable
{
    public const int DefaultMaxBodyBytes = 64 * 1024;
    public const int DefaultMaxConcurrentRequests = 16;

    private readonly HttpListener _listener = new();
    private readonly IModHttpHandlers _handlers;
    private readonly int _maxBodyBytes;
    private readonly SemaphoreSlim _requests;
    private readonly CancellationTokenSource _stop = new();
    private Task? _acceptLoop;
    private bool _disposed;

    public LoopbackHttpServer(
        int port,
        IModHttpHandlers handlers,
        int maxBodyBytes = DefaultMaxBodyBytes,
        int maxConcurrentRequests = DefaultMaxConcurrentRequests)
    {
        if (port is <= 0 or > 65535)
            throw new ArgumentOutOfRangeException(nameof(port));
        if (maxBodyBytes <= 0)
            throw new ArgumentOutOfRangeException(nameof(maxBodyBytes));
        if (maxConcurrentRequests <= 0)
            throw new ArgumentOutOfRangeException(nameof(maxConcurrentRequests));

        Port = port;
        Prefix = $"http://127.0.0.1:{port}/";
        _handlers = handlers;
        _maxBodyBytes = maxBodyBytes;
        _requests = new SemaphoreSlim(maxConcurrentRequests);
        _listener.Prefixes.Add(Prefix);
    }

    public int Port { get; }
    public string Prefix { get; }

    public void Start()
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        if (_acceptLoop is not null)
            throw new InvalidOperationException("HTTP server has already started.");
        _listener.Start();
        _acceptLoop = AcceptLoopAsync();
    }

    public void Dispose()
    {
        if (_disposed)
            return;
        _disposed = true;
        _stop.Cancel();
        _listener.Close();
    }

    private async Task AcceptLoopAsync()
    {
        while (!_stop.IsCancellationRequested)
        {
            HttpListenerContext context;
            try
            {
                context = await _listener.GetContextAsync().ConfigureAwait(false);
            }
            catch (Exception error) when (error is HttpListenerException or ObjectDisposedException or InvalidOperationException)
            {
                break;
            }
            _ = HandleAsync(context);
        }
    }

    private async Task HandleAsync(HttpListenerContext context)
    {
        if (!_requests.Wait(0))
        {
            await WriteAsync(
                context.Response,
                500,
                TechnicalErrorReply.Json("Too many concurrent HTTP requests to the Mod server.")).ConfigureAwait(false);
            return;
        }

        try
        {
            var request = context.Request;
            var path = request.Url is { Query.Length: 0 } url ? url.AbsolutePath : "";
            var body = "{}";

            if (request.HttpMethod == "POST")
            {
                if (request.ContentLength64 > _maxBodyBytes)
                {
                    await WriteAsync(
                        context.Response,
                        400,
                        InvalidRequestReply.Json("The request body exceeds the maximum allowed size.", "body")).ConfigureAwait(false);
                    return;
                }

                using var deadline = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token);
                deadline.CancelAfter(TimeSpan.FromSeconds(10));
                using var bytes = new MemoryStream();
                var buffer = new byte[4096];
                int count;
                while ((count = await request.InputStream.ReadAsync(buffer, deadline.Token).ConfigureAwait(false)) > 0)
                {
                    if (bytes.Length + count > _maxBodyBytes)
                    {
                        await WriteAsync(
                            context.Response,
                            400,
                            InvalidRequestReply.Json("The request body exceeds the maximum allowed size.", "body")).ConfigureAwait(false);
                        return;
                    }
                    bytes.Write(buffer, 0, count);
                }

                if (bytes.Length != 0)
                    body = Encoding.UTF8.GetString(bytes.ToArray());
            }

            var result = (request.HttpMethod, path) switch
            {
                ("GET", "/status") => _handlers.GetStatus(),
                ("GET", "/state") => _handlers.GetState(),
                ("POST", "/inspect") => await _handlers.Inspect(body).ConfigureAwait(false),
                ("POST", "/catalog") => await _handlers.Catalog(body).ConfigureAwait(false),
                ("POST", "/action") => _handlers.SubmitAction(body),
                ("POST", "/control") => _handlers.SetControl(body),
                ("POST", "/map/present") => await _handlers.PresentMap(body).ConfigureAwait(false),
                ("POST", "/map/restore/verify") => await _handlers.VerifyMapRestoration(body).ConfigureAwait(false),
                ("GET", "/operator/checkpoint/identity") => _handlers.GetCheckpointIdentity(),
                ("POST", "/operator/checkpoint") => _handlers.SubmitCheckpoint(body),
                ("POST", "/operator/eval/scenario") => await _handlers.PrepareLaboratoryScenario(body).ConfigureAwait(false),
                _ => new HttpReply(400, InvalidRequestReply.Json("Unknown HTTP route or method.", "route")),
            };

            await WriteAsync(context.Response, result.StatusCode, result.Json).ConfigureAwait(false);
        }
        catch (Exception)
        {
            await WriteAsync(
                context.Response,
                500,
                TechnicalErrorReply.Json("The native request could not be completed.")).ConfigureAwait(false);
        }
        finally
        {
            _requests.Release();
        }
    }

    private static async Task WriteAsync(HttpListenerResponse response, int status, string json)
    {
        try
        {
            response.StatusCode = status;
            response.ContentType = "application/json; charset=utf-8";
            var bytes = Encoding.UTF8.GetBytes(json);
            response.ContentLength64 = bytes.Length;
            await response.OutputStream.WriteAsync(bytes).ConfigureAwait(false);
            response.Close();
        }
        catch
        {
            try
            {
                response.Abort();
            }
            catch
            {
                // Ignore secondary socket abort failures.
            }
        }
    }
}
