"""Own the run's current strategy plan and retained resource notes."""

from sts2_harness.client import models as native
from sts2_harness.note_values import InitialNotes, NotesState, NotesView, owned_definition_ids


class Notes:
    def __init__(self, initial: InitialNotes) -> None:
        if isinstance(initial, str) and initial != "":
            raise ValueError("Text notes are unsupported; supply core, plan and other fields.")
        self._state = NotesState() if initial == "" else initial

    def read(self, observation: native.Observation | None = None) -> NotesView:
        if observation is not None:
            owned = owned_definition_ids(observation)
            self._state = self._state.model_copy(
                update={
                    "core": tuple(identity for identity in self._state.core if identity in owned)
                }
            )
        return NotesView(core=self._state.core, plan=self._state.plan, other=self._state.other)

    def write(self, fields: NotesState, supplied: set[str]) -> None:
        self._state = self._state.model_copy(
            update={name: getattr(fields, name) for name in supplied}
        )
