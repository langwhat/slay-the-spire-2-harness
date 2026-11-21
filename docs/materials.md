# Per-run documents

`EntryMaterials` pairs material settings with a document factory. The factory receives a `MaterialContext` and returns a `Documents` provider (see [EXTENSIONS.md](../EXTENSIONS.md)). The core calls it once for each run, including every new run a resident backend handles.

`MaterialContext` carries:

- `ascension`. Ordinary startup takes it from the Mod's `/status`, and headless startup takes it from its verified start recipe. It is `None` when unknown.
- `minimal`, which is true in minimal mode.

Use the context to pick or render material for the run, for example ascension-specific advice. Some guidelines:

- Keep selection logic, authoring formats, and curated knowledge in the extension.
- Return fully rendered `MaterialText`, so recordings and context versioning see exactly the text that was delivered.
- Serve explicit reads, subject lookups, and automatic entry guidance from the same provider.
- If a reader needs the ascension and it is `None`, raise `MaterialUnavailable` instead of guessing.

Evaluation code builds its documents with the case's verified ascension before passing them to the takeover or laboratory entry points. Initial guidance enters the role's system prompt; later new or changed guidance is appended under the usual context rules.
