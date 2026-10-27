# STS2 Harness

The no-save/load gameplay core provides an offline startup configuration check and a
Responses model client. The startup CLI does not contact game or model services; the
gameplay loop is not implemented yet.

Install Python 3.12+ and [uv](https://docs.astral.sh/uv/), then run:

```sh
uv sync
cp .env.example .env
# Edit .env once with your model and credentials.
uv run sts2 check-config
uv run sts2 check-config --set STS2_DRIVER=external
./scripts/check
```

Configuration is read once into an immutable snapshot shared by Run and Combat. Precedence
is `--set NAME=VALUE`, process environment, local `.env`, then code defaults.
`--env-file PATH` selects another defaults file; a missing file means no local defaults.
Overrides never modify the environment or defaults file. Dotenv interpolation is disabled.
Keep `.env` private; `.env.example` contains placeholders only. Unknown `LLM_`/`STS2_`
names in the local file fail validation; unrelated process environment keys are ignored.

| Variable | Default / meaning |
| --- | --- |
| `STS2_DRIVER` | `builtin`; `external` needs no model configuration |
| `LLM_PROTOCOL` | `responses`; also `chat-completions`, `messages` |
| `LLM_MODEL` | Required for builtin; configure once locally |
| `LLM_BASE_URL` | `https://api.openai.com/v1`; Messages uses `https://api.anthropic.com` |
| `LLM_AUTH` | `bearer` (`api-key` for Messages); also `none`, `api-key` (named header), `basic` |
| `LLM_API_KEY` | Bearer token or named-header credential |
| `LLM_AUTH_HEADER` | `x-api-key` for `api-key` |
| `LLM_USERNAME`, `LLM_PASSWORD` | Basic authentication credentials |
| `LLM_HEADERS` | Custom headers, one `Name: value` per line; last case-insensitive duplicate wins |
| `LLM_REASONING_EFFORT`, `LLM_SERVICE_TIER` | Optional provider-specific settings |
| `LLM_REQUEST_TIMEOUT_S` | `3600`, positive finite per-attempt timeout |
| `LLM_EXTRA_RETRIES` | `25`, nonnegative additional attempts |

Base URLs must omit userinfo, query strings, and fragments. Endpoint path prefixes are
retained when the Responses client appends `/responses`. Authentication applies SDK
defaults, configured authentication, then case-insensitive custom headers. SDK environment
credentials and custom headers do not supply project authentication.
`none` discards configured credentials and must not inherit SDK environment credentials.
External mode fixes the driver and skips unused model configuration validation. Custom
headers use LF-separated lines and ASCII field values with optional tabs; other control
characters are rejected. Bearer and named-header credentials must fit these HTTP header
constraints without leading/trailing whitespace. Basic passwords may contain Unicode.

The check emits only bounded configuration metadata. Free-form model IDs, endpoint URLs,
headers, effort, tier, and credentials are hidden, including on validation failures.
Success establishes offline validity only; provider-specific model/effort/tier support and
actual authentication require real endpoint checks.

Run Ruff, strict Pyright, and pytest with `./scripts/check` (requires `uv` on PATH).
Tests use temporary defaults and isolated process environments; no network or game runs.

The Python `sts2_harness.llm.client.Llm` client accepts a `ModelConfiguration` snapshot
and implements `complete(input, tools)` as an async context manager. It currently supports
Responses only. Callers own the immutable `ModelInput` message tuple; the client retains
no conversation. Tool definitions use concrete Pydantic argument models. Provider
schemas use `strict=False` to preserve optional-field semantics; tools remain responsible
for strict local argument decoding.

A completed `ModelReply` exposes read-only text/tool-call views, native continuation,
usage, and separate requested/response model metadata. `ResponsesReply.to_json()` and
`from_json()` preserve the complete ordered native output, including encrypted reasoning
and assistant phases in one immutable encoded snapshot. Decoded SDK items are independent
copies. Append the returned assistant message and associated tool-result
messages to the caller's input; native output is sent once, without `previous_response_id`.
The adapter does not execute tools or return intermediate streamed argument fragments.

Only `response.completed` with a completed response returns a reply. Premature clean EOF,
connection failures, timeouts, rate limits, and overload are retried. Each attempt gets its
own configured timeout covering request start through final decoding. SDK retries are
disabled; the default 25 additional attempts wait 2, 4, 8, 16, then 32 seconds, totaling
702 seconds of backoff. Explicit output truncation, authentication, permission, quota,
model/configuration errors, and malformed terminal responses raise a typed
`ModelUnavailable` without automatic regeneration. Cancellation propagates to the caller.

Missing usage counters remain `None`. Cache hit ratio uses cache-read tokens divided by
all input tokens, including cache writes. `Usage.summarize_cache()` sums comparable token
counts and reports coverage instead of averaging request percentages. Cache configuration
belongs to the protocol adapter; no separate cache settings or warmup calls are introduced.

Responses validation uses the official SDK with fake HTTP and controlled waits. It does
not establish real endpoint compatibility or game behavior. Inject an HTTP transport and
sleep callable into `Llm` to exercise these public boundaries without network calls.
