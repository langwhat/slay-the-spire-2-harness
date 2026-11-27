"""Smoke tests for the runnable examples/custom-knowledge provider."""

import asyncio
import importlib.util
from pathlib import Path

from sts2_harness.client.models import DecisionOwner
from sts2_harness.entry_materials import EntryMaterials, MaterialContext
from sts2_harness.knowledge import MaterialRole, SkillRead, SubjectId

_EXAMPLE = Path(__file__).resolve().parent.parent / "examples/custom-knowledge/play_with_guides.py"


def _load_materials() -> EntryMaterials:
    spec = importlib.util.spec_from_file_location("custom_knowledge_example", _EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    builder = getattr(module, "build_entry_materials", None)
    assert callable(builder)
    materials = builder()
    assert isinstance(materials, EntryMaterials)
    return materials


def test_custom_knowledge_example_lists_reads_and_matches_subjects() -> None:
    materials = _load_materials()
    assert materials.settings.entry_documents_enabled is True
    documents = materials.documents(MaterialContext(ascension=10, minimal=True))

    async def exercise() -> None:
        listed = await documents.list(DecisionOwner.RUN)
        assert {item.id for item in listed} == {"guide:ironclad", "boss:queen", "card:bash"}

        boss_view = await documents.read(SkillRead(id="boss:queen"))
        assert "ENCOUNTER.QUEEN_BOSS" in boss_view.text.body

        matches = await documents.lookup(
            (
                SubjectId(category="character", definition_id="CHARACTER.IRONCLAD"),
                SubjectId(category="card", definition_id="CARD.BASH"),
            )
        )
        by_id = {doc.id: doc for doc in matches}
        assert set(by_id) == {"guide:ironclad", "card:bash"}
        assert by_id["guide:ironclad"].auto_roles == (MaterialRole.RUN, MaterialRole.DECK)
        assert by_id["card:bash"].auto_roles == (MaterialRole.DECK, MaterialRole.COMBAT)

    asyncio.run(exercise())
