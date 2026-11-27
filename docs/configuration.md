# Configuration

Each backend process reads its configuration once into an immutable snapshot, and Run, Deck, and Combat all use it. To change a setting, restart the process.

Precedence, highest first:

1. `--set NAME=VALUE` on the command line
2. the process environment
3. the local `.env` file (or the file given by `--env-file PATH`; a missing file means no local defaults)
4. code defaults

Overrides never modify the environment or the `.env` file. Dotenv interpolation is off. Keep `.env` private; `.env.example` holds placeholders only. Unknown `LLM_`/`STS2_` names in the file are rejected; unrelated environment variables are ignored.

Run `uv run sts2 check-config` to validate settings offline. It prints only safe metadata and hides model IDs, URLs, headers, effort, tier, and credentials, even on errors. It does not contact the endpoint, so it cannot tell you whether the provider accepts your model, effort, or credentials.

| Variable | Default and meaning |
| --- | --- |
| `STS2_DRIVER` | `builtin`. `external` runs the [external tool service](running.md#external-gameplay-tools) and needs no model settings. |
| `STS2_USER_INPUT` | `false`. `true` enables [live user input](running.md#live-user-input). |
| `STS2_MINIMAL` | `true` for builtin driving (`false` for `external`). Set `false` to switch from [minimal mode](#minimal-mode) to full mode. |
| `LLM_PROTOCOL` | `responses`. Also `chat-completions` or `messages`. |
| `LLM_MODEL` | Required for builtin driving. |
| `LLM_BASE_URL` | `https://api.openai.com/v1`; `https://api.anthropic.com` for Messages. |
| `LLM_AUTH` | `bearer` (`api-key` for Messages). Also `none`, `api-key` (named header), or `basic`. |
| `LLM_API_KEY` | Bearer token or named-header credential. |
| `LLM_AUTH_HEADER` | Header name for `api-key`; default `x-api-key`. |
| `LLM_USERNAME`, `LLM_PASSWORD` | Basic authentication. |
| `LLM_HEADERS` | Extra headers, one `Name: value` per line. The last case-insensitive duplicate wins. |
| `LLM_REASONING_EFFORT`, `LLM_SERVICE_TIER` | Optional, passed to the provider as is. One effort applies to the whole run. |
| `LLM_REQUEST_TIMEOUT_S` | `3600`. Timeout for each attempt. |
| `LLM_EXTRA_RETRIES` | `25`. Retries after the first failed attempt. |
| `LLM_CONTEXT_WINDOW` | `300000`. Input window used for [capacity compaction](#input-size-estimate). |
| `LLM_CAPACITY_VERIFIED_ENDPOINT` | Empty. See below. |

## Endpoints and authentication

Base URLs must not contain userinfo, query strings, or fragments. Path prefixes are kept; the client appends `/responses`, `/chat/completions`, or `/v1/messages`.

Common `.env` templates:

```dotenv
# OpenAI Responses
LLM_PROTOCOL=responses
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=your-model-id
LLM_AUTH=bearer
LLM_API_KEY=replace-locally
```

```dotenv
# OpenAI-compatible Chat Completions (third-party endpoints / proxies)
LLM_PROTOCOL=chat-completions
LLM_BASE_URL=https://your-provider.example/v1
LLM_MODEL=your-model-id
LLM_AUTH=bearer
LLM_API_KEY=replace-locally
```

```dotenv
# Anthropic Messages
LLM_PROTOCOL=messages
LLM_BASE_URL=https://api.anthropic.com
LLM_MODEL=your-model-id
LLM_AUTH=api-key
LLM_API_KEY=replace-locally
```

Headers are built in this order: SDK defaults, then configured authentication, then custom headers (case-insensitive). Credentials from SDK environment variables are never used. `LLM_AUTH=none` sends no credentials at all. Custom header lines are LF-separated, with ASCII values and optional tabs. Bearer and named-header credentials must be valid header values without surrounding whitespace. Basic passwords may contain Unicode.

### Context-too-large signal

Some Responses endpoints reject an oversized request with HTTP 400 and the code `context_too_large`. If you have confirmed your endpoint does this, set `LLM_CAPACITY_VERIFIED_ENDPOINT` to exactly the same value as `LLM_BASE_URL`. The core then treats that rejection as a capacity signal. Changing `LLM_BASE_URL` turns the signal off until the two match again. Recordings keep the endpoint category and signal, not the URL.

## Minimal mode

Minimal mode is the default for builtin driving (`STS2_MINIMAL=true`). It skips post-battle reviews, separate Run handoff turns, and cross-node strategy notes so decisions settle faster with fewer pauses between moves. Switch to full mode with `STS2_MINIMAL=false` in `.env` or `--set STS2_MINIMAL=false`. `--minimal` on `start` or `headless` still forces minimal mode on and beats every other source. `check-config` shows the resolved mode.

Both modes share the same core gameplay, state-reading, and Deck drafting rules, plus automatic progression, deck cycle statistics, native mechanism extraction, native catalog queries (`run_knowledge` / `combat_knowledge`), and automatic knowledge injection. Full mode adds post-battle combat reviews, cross-node strategy notes, `run_notes` / `*_inspect` / `*_skill` tools, and external document supplements on knowledge lookups—which may improve win rate at the cost of extra model calls and slower pacing.

Fewer input tokens do not always mean lower cost when cached input is discounted, so compare total usage cost. The archive records the mode, so offline replay uses the same behavior.

## Input-size estimate

Capacity compaction needs an estimate of each request's input size. It runs locally, with no tokenizer download or provider counting call. The default estimator, `reported_usage_bytes_v1`, works like this:

- When the previous request in the same context reported its usage, the estimate is that request's input tokens plus output tokens, plus the bytes added since then divided by 4.
- Otherwise it is the JSON byte size of the request's input fields divided by 4.

When the estimate reaches 90% of `LLM_CONTEXT_WINDOW`, the agent tries a compacted input; see [architecture](architecture.md#context-and-model-input). The Journal records the estimator, the capacity policy, and each attempt's estimate and reported usage. Older archives may use the earlier `input_characters_v1` estimator, and replay keeps whichever one they recorded.

Use `uv run --offline` when uv itself must not touch the network. Dependencies must already be cached.
