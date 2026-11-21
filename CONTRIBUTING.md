# Contributing

## Before you start

- Open an issue and agree on changes to interfaces, architecture, prompts, model-visible formats, or gameplay behavior with the maintainer before writing code.
- Open a pull request directly for typos, broken links, or straightforward bug fixes.
- Read the relevant parts of [docs/architecture.md](docs/architecture.md) and [GLOSSARY.md](GLOSSARY.md).
- Keep each change small enough to verify on its own.
- Do not commit private strategy knowledge, extracted game data, credentials, saves, or run archives.

## Code

- Python 3.12+, uv, Ruff, strict Pyright, and pytest.
- Prefer deep modules with clear state ownership and narrow interfaces. Use composition, guard clauses, and concrete parameter and return types.
- Use enums for closed sets of domain values.
- Decode external data into typed values at IO boundaries. Keep unavoidable dynamic third-party values inside typed adapters; do not use `Any` or loose data bags in project-owned code.
- Build for the consumers that exist today. Skip speculative frameworks, compatibility paths, and unrelated cleanup.
- Name things for what they do. Write comments only for constraints or reasons the code cannot show.
- Code, comments, prompts, docs, and commit messages are in English.

## Tests and checks

- Run `./scripts/check` (Ruff, Pyright, pytest). If you touch `mod/`, also run `./scripts/check-mod`.
- Test observable behavior at public boundaries; the agent loop is tested against fake HTTP. Add tests that protect real behavior or reproduce a real bug. Do not add coverage targets, tests that assert prompt substrings, or tests that restate the implementation.
- Report each kind of evidence separately: static checks, fake HTTP tests, Mod builds, real model calls, and real game runs. Each proves something different. Say which revision you tested, which commands you ran, and what you could not check.
- Use isolated headless instances for real runs, and leave user data alone.
- If you change prompts, tool descriptions, tool results, or injected material, read the changed text as the model will see it, inside a representative assembled request, and check that it is accurate and relevant.

## Review and commits

- Someone other than the author reviews each change against its issue and these rules. A human or an AI-assisted reviewer is fine. The maintainer decides what to accept.
- When combining concurrent changes, resolve conflicts, rerun the affected checks, and get a fresh review if behavior changed.
- Make one focused commit per step. Write a short English title that describes the resulting behavior; add a body only when the reason or a constraint needs explaining. Leave out AI attribution, development history, and "tests pass" claims; put evidence in the PR or review instead.
- Fix shared history with new commits, not rewrites.

## Pull requests

Include:

- **Summary:** what changes and why.
- **Evidence:** the tested revision, commands, results, and known limitations.
- **Merge danger:** how reversible the change is and what it affects.

A small diagram, diff sketch, or pseudocode helps when the change is hard to see from the diff. Link the issue. If a PR depends on another open PR, say so and keep it in draft until the other one is ready.
