"""Own strategy fields and the identities of wants available before each offer."""

import re
from dataclasses import dataclass

from pydantic import MISSING

from sts2_harness.client import models as native
from sts2_harness.note_values import Engine, InitialNotes, NotesState, NotesView, NumberedWant


@dataclass(frozen=True, slots=True)
class WantIdentity:
    text: str
    recorded_at: int


class Notes:
    def __init__(self, initial: InitialNotes) -> None:
        if isinstance(initial, str) and initial != "":
            raise ValueError("Legacy text notes are unsupported; supply structured notes.")
        self._state = NotesState() if initial == "" else initial
        self._clock = 0
        self._expired: set[WantIdentity] = set()
        self._wants: tuple[WantIdentity, ...] = ()
        self._offer_wants: dict[
            tuple[str, str | None, str | None, tuple[str, ...]], frozenset[WantIdentity]
        ] = {}
        self._offer_keys: dict[
            tuple[str, str | None, str | None, str],
            tuple[str, str | None, str | None, tuple[str, ...]],
        ] = {}
        self._set_wants(self._state.wants)

    def _set_wants(self, sentences: tuple[str, ...]) -> None:
        previous = {want.text: want for want in self._wants}
        identities: list[WantIdentity] = []
        for text in sentences:
            identity = previous.get(text)
            if identity is None:
                self._clock += 1
                identity = WantIdentity(text, self._clock)
            identities.append(identity)
        self._wants = tuple(identities)

    def read(self) -> NotesView:
        return NotesView(
            engine=self._state.engine,
            wants=tuple(
                NumberedWant(
                    number=number,
                    text=want.text,
                    expired=True if want in self._expired else MISSING,
                )
                for number, want in enumerate(self._wants, 1)
            ),
            filler=self._state.filler,
            evidence=self._state.evidence,
            other=self._state.other,
        )

    def write(self, fields: NotesState, supplied: set[str]) -> None:
        if "wants" in supplied:
            self._set_wants(fields.wants)
        self._state = self._state.model_copy(
            update={name: getattr(fields, name) for name in supplied}
        )

    def finish_encounter(self, definition_id: str) -> None:
        self._expired.update(
            want
            for want in self._wants
            if definition_id in re.findall(r"ENCOUNTER\.[A-Z0-9_]+", want.text)
        )

    def observe(self, observation: native.Observation) -> None:
        state = observation.state
        if not isinstance(state, native.RunState):
            return
        location = observation.location
        node = (location.game_instance_id, location.run_id, location.node_id)
        for offer in state.offers:
            if offer.kind is not native.OfferKind.CARD:
                continue
            # Opening an ordinary reward publishes another offer for the same
            # candidates. Keep its original pre-offer commitment boundary.
            key = (*node, tuple(sorted(offer.contents)))
            self._offer_wants.setdefault(key, frozenset(self._wants))
            self._offer_keys[(*node, offer.ref)] = key

    def usable_wants(self, observation: native.Observation, offer: str) -> tuple[int, ...]:
        location = observation.location
        key = self._offer_keys.get(
            (location.game_instance_id, location.run_id, location.node_id, offer)
        )
        available = (
            self._offer_wants.get(key, frozenset[WantIdentity]())
            if key is not None
            else frozenset[WantIdentity]()
        )
        return tuple(
            number
            for number, want in enumerate(self._wants, 1)
            if want in available and want not in self._expired
        )

    def want(self, number: int) -> WantIdentity | None:
        return self._wants[number - 1] if 1 <= number <= len(self._wants) else None

    def consume(self, want: WantIdentity) -> None:
        self._wants = tuple(value for value in self._wants if value != want)
        self._state = self._state.model_copy(update={"wants": tuple(w.text for w in self._wants)})

    def pivot(self, engine: Engine) -> None:
        self._state = self._state.model_copy(update={"engine": engine})
