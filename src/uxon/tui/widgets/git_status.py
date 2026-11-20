"""Compact Git status that stays hidden until there is something to show."""

from textual.widgets import Static


class GitStatus(Static):
    def __init__(self) -> None:
        super().__init__("", id="git-status", markup=False)
        self.display = False

    def set_status(self, text: str) -> None:
        if self.content == text:
            return
        self.display = bool(text)
        self.update(text)
