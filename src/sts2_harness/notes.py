"""Own the run's current strategy plan and retained resource notes."""

from sts2_harness.note_values import InitialNotes, NotesState, NotesView


class Notes:
    def __init__(self, initial: InitialNotes) -> None:
        if isinstance(initial, str) and initial != "":
            raise ValueError("Text notes are unsupported; supply plan and other fields.")
        self._state = NotesState() if initial == "" else initial

    def read(self) -> NotesView:
        return NotesView(plan=self._state.plan, other=self._state.other)

    def write(self, fields: NotesState, supplied: set[str]) -> None:
        self._state = self._state.model_copy(
            update={name: getattr(fields, name) for name in supplied}
        )
