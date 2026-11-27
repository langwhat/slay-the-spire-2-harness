# Custom knowledge example

A minimal, runnable example showing how to plug your own Markdown strategy guides into `sts2-harness` using `FileDocuments` and `EntryMaterials`.

## Layout

```text
examples/custom-knowledge/
├── guides/
│   ├── ironclad.md       # Character macro guide (Run + Deck)
│   ├── queen-boss.md     # Boss gameplan (Run + Deck + Combat)
│   └── bash.md           # Card notes (Deck + Combat)
└── play_with_guides.py   # Entry point wrapping sts2_harness.cli.main
```

## Run

Start the Mod-enabled game, configure `.env` at the repository root, then run:

```console
uv run python examples/custom-knowledge/play_with_guides.py start
```

All normal `sts2` CLI flags (`--env-file`, `--set`, `--mod-url`, `check-config`, etc.) work the same way. See [docs/custom-knowledge.md](../../docs/custom-knowledge.md) for how subject matching, `MaterialContext`, and offline verification work.
