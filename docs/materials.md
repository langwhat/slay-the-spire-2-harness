# Run-scoped supplementary documents

`EntryMaterials` contains material-selection settings and a document factory. The
factory accepts a `MaterialContext` and returns the existing `Documents` interface.
Interactive startup supplies the native run ascension published by `/status`;
headless startup supplies the ascension from its verified native start recipe.
A new factory result is created for each run, including successive runs handled
by a resident backend. No model-visible protocol changes are required.

Extensions can bind a document reader to that immutable context. Keep numerical
selection, authoring formats, and curated game knowledge in the extension. Return
fully rendered `MaterialText` so recording and Context versioning operate on the
text actually delivered. Explicit reads, subject lookup, and automatic entry
guidance must use the same reader. Unknown ascension is `None`; a reader that
requires it must report `MaterialUnavailable` instead of assuming a difficulty.

Evaluation callers create their document dependencies with the verified case's
ascension before supplying them to the existing takeover or laboratory entry.
The document protocol, recorded material replies, and replayed model input retain
their existing formats. Initial guidance enters the role's system context; later
first-seen or changed guidance is appended using the existing Context rules.
