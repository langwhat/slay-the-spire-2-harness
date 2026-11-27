"""Minimal launcher that plugs local Markdown strategy guides into sts2-harness."""

import sys
from collections.abc import Sequence
from pathlib import Path

from sts2_harness.cli import main as harness_main
from sts2_harness.entry_materials import EntryMaterials, MaterialContext
from sts2_harness.journal import SessionMaterials
from sts2_harness.knowledge import MaterialRole, SubjectId
from sts2_harness.materials import DocumentSource, FileDocuments

GUIDES = Path(__file__).parent / "guides"


def build_documents(context: MaterialContext) -> FileDocuments:
    """Build the per-run document provider from local Markdown files."""
    _ = context
    sources = (
        DocumentSource(
            id="guide:ironclad",
            name="Ironclad Macro Guide",
            description="Act-by-act drafting and pathing priorities for Ironclad.",
            path=GUIDES / "ironclad.md",
            subjects=(SubjectId(category="character", definition_id="CHARACTER.IRONCLAD"),),
            auto_roles=(MaterialRole.RUN, MaterialRole.DECK),
        ),
        DocumentSource(
            id="boss:queen",
            name="Queen Boss Plan",
            description="Preparation and combat priorities for ENCOUNTER.QUEEN_BOSS.",
            path=GUIDES / "queen-boss.md",
            subjects=(SubjectId(category="encounter", definition_id="ENCOUNTER.QUEEN_BOSS"),),
            auto_roles=(MaterialRole.RUN, MaterialRole.DECK, MaterialRole.COMBAT),
        ),
        DocumentSource(
            id="card:bash",
            name="Bash Notes",
            description="Upgrade priority and Vulnerable setup notes for CARD.BASH.",
            path=GUIDES / "bash.md",
            subjects=(SubjectId(category="card", definition_id="CARD.BASH"),),
            auto_roles=(MaterialRole.DECK, MaterialRole.COMBAT),
        ),
    )
    return FileDocuments(sources)


def build_entry_materials() -> EntryMaterials:
    """Pair the document factory with its session delivery settings."""
    return EntryMaterials(
        documents=build_documents,
        settings=SessionMaterials(
            entry_documents_enabled=True,
            act_guidance={},
            battle_guidance=(),
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    return harness_main(
        sys.argv[1:] if argv is None else argv,
        materials=build_entry_materials(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
