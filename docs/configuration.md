# Configuration

Configuration is read once into an immutable snapshot shared by Run and Combat. Precedence
is `--set NAME=VALUE`, process environment, local `.env`, then code defaults.
`--env-file PATH` selects another defaults file; a missing file means no local defaults.
Overrides never modify the environment or defaults file. Dotenv interpolation is disabled.
Keep `.env` private; `.env.example` contains placeholders only. Unknown `LLM_`/`STS2_`
names in the local file fail validation; unrelated process environment keys are ignored.

| Variable | Default / meaning |
| --- | --- |
| `STS2_DRIVER` | `builtin`; `external` needs no model configuration |
| `STS2_USER_INPUT` | `false`; `true` enables [live user input](running.md#live-user-input) |
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
| `LLM_CONTEXT_WINDOW` | `300000`, positive input accounting window |
| `LLM_CAPACITY_VERIFIED_ENDPOINT` | Empty by default; an exact match with `LLM_BASE_URL` enables the verified Responses HTTP 400 `context_too_large` rejection signal |

Set `LLM_CAPACITY_VERIFIED_ENDPOINT` only after verifying that exact endpoint's structured
Responses rejection. Changing `LLM_BASE_URL` disables the signal until the selector matches
again. Other protocols, HTTP statuses, and streamed failures do not establish this rejection.
Recordings retain the verified category, protocol, and signal; they do not store the endpoint URL.

Base URLs must omit userinfo, query strings, and fragments. Endpoint path prefixes are
retained when the selected client appends `/responses` or `/chat/completions`.
Authentication applies SDK
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

## Minimal mode

Add `--minimal` to `start` or `headless` for minimal gameplay, or set
`STS2_MINIMAL=true` in `.env` or the process environment. As with other settings, `--set`
overrides the process environment, which overrides `.env`; explicit `--minimal` takes
precedence over all three. Use `--set STS2_MINIMAL=false` to disable an environment default.
`check-config` reports the resolved mode. Both modes share gameplay and state-reading rules.
Ordinary mode adds deck construction and battle review instructions, notes, and query tools.
Minimal gameplay exposes only actions and route planning, skips model battle reviews and
standalone report-processing calls, and supplies no notes. Automatic knowledge injection,
measured deck statistics, and automatic progression remain available.
Minimal gameplay targets lower total usage cost; fewer input tokens alone do not establish
savings when cached input is discounted. The archive records the mode so offline replay
uses the same behavior. Ordinary gameplay is the default.

## Input-size estimate

The `input_characters_v1` estimator runs locally without tokenizer packages, downloaded
tables, provider counting calls, or historical-usage calibration. It counts Unicode
characters in decoded string values plus a canonical JSON structural skeleton whose
string values are empty. Keys, non-string scalar values, and structure count once.
The estimate is `ceil(characters / 3) + 3 * items`, where items are top-level entries
in the protocol's input-bearing arrays and tool list: Responses `input`/`tools`, Chat
Completions `messages`/`tools`, and Messages `system`/`messages`/`tools`.
Opaque continuation strings count at ordinary character length. HTTP escaping and
transport-only settings do not inflate the estimate. Full canonical request digests
and byte counts cover the unchanged transport body. The algorithm identity is recorded
in the Journal alongside the capacity policy and each attempt's estimate and provider usage.
These are approximate counts, not provider counts or a guaranteed upper bound, especially
for different languages, content distributions, or providers. Use `uv run --offline`
when uv itself must not access the network; dependencies must already be cached.
