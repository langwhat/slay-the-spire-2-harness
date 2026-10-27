"""The run's strategy notes are authoritative in memory."""

from sts2_harness.results import NotesUpdated, Reason, ReasonCode, Rejected, Suggestion


class Notes:
    def __init__(self, initial: str) -> None:
        self._text = initial

    def read(self) -> str:
        return self._text

    def replace(self, text: str) -> NotesUpdated:
        self._text = text
        return NotesUpdated()

    def edit(self, old_text: str, new_text: str) -> NotesUpdated | Rejected:
        position = self._text.find(old_text)
        if position < 0 or self._text.find(old_text, position + 1) >= 0:
            return Rejected(
                reason=Reason(
                    code=ReasonCode.NOTES_CONFLICT,
                    message="The existing fragment must match exactly once; notes are unchanged.",
                ),
                suggested_actions=(
                    Suggestion(message="Read the notes and choose one unique fragment."),
                ),
            )
        self._text = self._text[:position] + new_text + self._text[position + len(old_text) :]
        return NotesUpdated()
