"""Immutable observations; paths identify entries, including deleted entries."""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class FileEvent:
    event_type: str
    watched_folder: Path
    path: Path
    observed_at: datetime

    def __post_init__(self):
        if self.event_type not in ("new", "modified", "deleted"):
            raise ValueError("Unsupported file-event type")
        if not isinstance(self.observed_at, datetime):
            raise TypeError("File-event observed_at must be a datetime")
        folder = Path(self.watched_folder).resolve()
        path = Path(self.path)
        if not path.is_absolute() or path.parent != folder:
            raise ValueError("File-event path must be an immediate entry of its watched folder")
        object.__setattr__(self, "watched_folder", folder)
        object.__setattr__(self, "path", path)
