"""Answer knowledge queries from a declared immutable native catalog snapshot."""

from typing import Self
from unicodedata import category

from pydantic import MISSING, model_validator

from sts2_harness.client import models as native


class SnapshotIncomplete(RuntimeError):
    """The snapshot does not establish the requested native fact."""


class FrozenSearchEntry(native.WireModel):
    definition_id: native.Nonempty
    name: native.Nonempty
    text: str | None


class FrozenSearchIndex(native.WireModel):
    source: native.Nonempty
    entries: tuple[FrozenSearchEntry, ...]


class FrozenCatalog(native.WireModel):
    complete: bool
    catalog: native.CatalogData
    definitions: tuple[native.NativeDefinition, ...]
    search_index: FrozenSearchIndex | None = None

    @model_validator(mode="after")
    def unique_definitions(self) -> Self:
        ids = tuple(definition.definition_id for definition in self.definitions)
        if len(ids) != len(set(ids)):
            raise ValueError("Native snapshot contains duplicate definition IDs.")
        if self.search_index is not None:
            search_ids = tuple(entry.definition_id for entry in self.search_index.entries)
            if len(search_ids) != len(set(search_ids)) or set(search_ids) != set(ids):
                raise ValueError("Native search index must cover each definition exactly once.")
        return self

    async def query(
        self, request: native.CatalogRequest
    ) -> native.CatalogCompleted | native.CatalogRejected:
        if isinstance(request, native.CatalogQuery):
            return native.CatalogCompleted(status=native.ResultStatus.COMPLETED, data=self.catalog)
        if isinstance(request, native.LookupQuery):
            index = {entry.definition_id: entry for entry in self.definitions}
            missing = tuple(identity for identity in request.ids if identity not in index)
            if missing and not self.complete:
                raise SnapshotIncomplete(
                    "The snapshot does not contain every requested definition."
                )
            return native.CatalogCompleted(
                status=native.ResultStatus.COMPLETED,
                data=native.LookupData(
                    entries=tuple(index[identity] for identity in request.ids if identity in index),
                    missing_ids=missing,
                ),
            )
        if not self.complete:
            raise SnapshotIncomplete("Search requires a complete native catalog snapshot.")
        if request.category is not MISSING and request.category not in self.catalog.categories:
            return native.CatalogRejected(
                status=native.ResultStatus.REJECTED,
                error=native.CatalogRejection(
                    code=native.ErrorCode.UNSUPPORTED_CATEGORY,
                    message="This native category is not supported.",
                ),
            )
        if request.filters is not MISSING and not self._known_filters(request.filters):
            return native.CatalogRejected(
                status=native.ResultStatus.REJECTED,
                error=native.CatalogRejection(
                    code=native.ErrorCode.INVALID_FILTER,
                    message="A card filter contains an unknown value.",
                ),
            )
        text = request.text if request.text is not MISSING else ""
        if any(not _ascii_id(entry.definition_id) for entry in self.definitions):
            raise SnapshotIncomplete("Native ordinal search cannot be established for this text.")
        if text and self.search_index is None:
            raise SnapshotIncomplete("Search requires the complete native raw search index.")
        index = (
            {entry.definition_id: entry for entry in self.search_index.entries}
            if self.search_index is not None
            else {}
        )
        if text and (
            not _search_text_is_supported(text)
            or any(
                not _search_text_is_supported(entry.name)
                or not _search_text_is_supported(entry.text or "")
                for entry in index.values()
            )
        ):
            raise SnapshotIncomplete("Native ordinal search cannot be established for this text.")
        terms = tuple(term.casefold() for term in text.split())
        matches = [
            entry
            for entry in self.definitions
            if (request.category is MISSING or entry.category == request.category)
            and self._matches_filters(entry, request.filters)
            and all(
                term in entry.definition_id.casefold()
                or term in index[entry.definition_id].name.casefold()
                or term in (index[entry.definition_id].text or "").casefold()
                for term in terms
            )
        ]

        def rank(entry: native.NativeDefinition) -> tuple[int, str]:
            if not text:
                return (1, entry.definition_id)
            identity, name = (
                entry.definition_id.casefold(),
                index[entry.definition_id].name.casefold(),
            )
            if text.casefold() in (identity, name):
                return (0, entry.definition_id)
            if all(term in identity or term in name for term in terms):
                return (1, entry.definition_id)
            return (2, entry.definition_id)

        matches.sort(key=rank)
        offset = request.offset if request.offset is not MISSING else 0
        limit = request.limit if request.limit is not MISSING else 5
        page = tuple(self._match(entry) for entry in matches[offset : offset + limit])
        next_offset = offset + len(page)
        return native.CatalogCompleted(
            status=native.ResultStatus.COMPLETED,
            data=native.SearchData(
                matches=page,
                total=len(matches),
                next_offset=next_offset if next_offset < len(matches) else None,
            ),
        )

    def _known_filters(self, filters: native.CardFilters) -> bool:
        available = self.catalog.card_filters
        return (
            _known(filters.pools, tuple(item.value for item in available.pools))
            and _known(filters.types, tuple(item.value for item in available.types))
            and _known(filters.rarities, tuple(item.value for item in available.rarities))
            and _known(filters.energy_costs, available.energy_costs)
        )

    @staticmethod
    def _matches_filters(
        entry: native.NativeDefinition, filters: native.CardFilters | MISSING
    ) -> bool:
        if filters is MISSING:
            return True
        if not isinstance(entry, native.CardDefinition):
            return False
        card = entry.native
        return (
            _includes(filters.pools, card.card_pool)
            and _includes(filters.types, card.card_type)
            and _includes(filters.rarities, card.rarity)
            and _includes(filters.energy_costs, card.cost.energy)
        )

    @staticmethod
    def _match(entry: native.NativeDefinition) -> native.NativeMatch:
        if isinstance(entry, native.CardDefinition):
            return native.CardMatch(
                category=native.NativeCategory.CARD,
                definition_id=entry.definition_id,
                name=entry.name,
                description=entry.native.text,
                card_type=entry.native.card_type,
                rarity=entry.native.rarity,
                cost=entry.native.cost,
            )
        return native.NonCardMatch(
            category=entry.category,
            definition_id=entry.definition_id,
            name=entry.name,
            description=entry.native.text,
        )


def _known[T](values: tuple[T, ...] | MISSING, allowed: tuple[T, ...]) -> bool:
    return values is MISSING or all(value in allowed for value in values)


def _includes[T](values: tuple[T, ...] | MISSING, value: object) -> bool:
    return values is MISSING or value in values


_SEARCH_PUNCTUATION = frozenset("。、，！（）“”？：～")


def _search_text_is_supported(value: str) -> bool:
    for character in value:
        code = ord(character)
        if code < 128 and not 0x1C <= code <= 0x1F:
            continue
        if character in _SEARCH_PUNCTUATION:
            continue
        if (0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF) and category(character) == "Lo":
            continue
        return False
    return True


def _ascii_id(value: str) -> bool:
    return all(0x20 <= ord(character) < 0x7F for character in value)
