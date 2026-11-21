# Contributing

## Scope and design

Start from an agreed issue and the relevant architecture and domain contracts. Keep changes small and independently verifiable. Confirm changes to public interfaces, model-visible formats, prompts, or product behavior before implementing them. Do not copy previous implementations or include private strategy knowledge, generated game datasets, credentials, saves, or runtime archives in the repository.

Use deep modules with clear state ownership and narrow interfaces. Prefer composition, guard clauses, concrete parameter and return types, and dedicated enums for closed domain values. Validate external input at IO boundaries. Keep unavoidable third-party dynamic values inside typed adapters; do not use `Any` or arbitrary data bags in project-owned contracts.

Choose abstractions for current consumers. Avoid speculative frameworks, compatibility paths, and unrelated cleanup. Names should explain behavior; comments should explain necessary constraints or non-obvious reasons. Use English for code, comments, prompts, project documentation, and commit messages.

## Implementation and validation

Use Python 3.12+, uv, Ruff, strict Pyright, and pytest. Run the repository's applicable configured checks and test observable behavior at useful public boundaries. Add only tests that protect meaningful behavior or a demonstrated defect. Do not add coverage targets, prompt-substring assertions, or tests that merely repeat implementation details.

Keep validation claims distinct: static checks, fake HTTP or recorded-dependency tests, Mod builds, actual model endpoint checks, and real game runs prove different things. Record the tested revision, relevant commands, results, and limitations. Use isolated developer resources for real runs and preserve user data. A missing required environment is an unverified requirement, not a passing check.

Review changed prompts, tool descriptions/results, and injected materials in representative assembled runtime input. Check their meaning, relevance, permissions, and state accuracy; tests of arbitrary wording do not substitute for that review.

## Review and integration

Have someone other than the author review the change against its issue and coding standards. Reviewers may be human or agent-assisted; no particular model or paid service is required. The maintainer remains responsible for accepting the change and deciding which findings require action.

Validate the combined result when integrating concurrent changes. Resolve conflicts, rerun affected checks, and refresh review when behavior changes. Use an explicit reviewed revision as the integration target. Preserve unrelated work and do not push or publish without the relevant authorization.

Make one focused commit per small implementation step. Include local corrections before finalizing that step; corrections to already shared changes use additional focused commits. Use a concise English title describing the resulting behavior, with a body only when rationale or constraints help. Omit AI attribution, development-history narration, and test-passing claims from commit messages; put validation evidence in the review report.

When using a pull request, provide a concise Summary, concrete Evidence, and Merge Danger (reversibility and affected area). Use a small diagram, diff sketch, or pseudocode only where it helps explain the change. Include relevant issue/acceptance context, tested revision, and real limitations; a new feature need not have an invented failing-before test. Declare dependencies on other open PRs and keep dependent PRs draft until their target is ready. Follow the maintainer's explicit delivery policy for merge/push authority and human review timing. Direct batch publication may be delegated after independent validation; otherwise submit a PR and leave integration to the maintainer. Keep fixed commit ranges and evidence for later review, and correct published work through new commits. Asynchronous feedback does not waive explicit design approvals. Local worktree scheduling, reviewer choices, and private automation configuration are not contributor requirements.
