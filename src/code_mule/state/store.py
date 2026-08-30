"""Atomic JSON file storage for project-state snapshots."""

from contextlib import suppress
import json
import os
from pathlib import Path
import tempfile
from typing import cast

from .models import ProjectState
from .serialization import (
    InvalidProjectState,
    deserialize_project_state,
    serialize_project_state,
)


class ProjectStateNotFound(FileNotFoundError):
    """Raised when the configured project-state file does not exist."""


class JsonProjectStateStore:
    """Persist one complete ProjectState snapshot as an atomic JSON file."""

    def __init__(self, path: Path):
        self._path = path

    def save(self, state: ProjectState) -> None:
        payload = serialize_project_state(state)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(
                    payload,
                    temporary_file,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self._path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                with suppress(OSError):
                    temporary_path.unlink()

    def load(self) -> ProjectState:
        try:
            with self._path.open("r", encoding="utf-8") as state_file:
                payload: object = json.load(state_file)
        except FileNotFoundError as error:
            raise ProjectStateNotFound(
                f"project state file not found: {self._path}"
            ) from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise InvalidProjectState("project state file is not valid JSON") from error

        return deserialize_project_state(cast(dict[str, object], payload))

    def exists(self) -> bool:
        return self._path.exists()


__all__ = ["JsonProjectStateStore", "ProjectStateNotFound"]
