"""Walks a cloned repository and builds a searchable index of its files.

The walk is bounded, does not follow symbolic links, and skips directories that
hold dependencies or build output — vendored code would otherwise dominate
every count the detector derives from this index.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Final

from app.core.config import Settings, get_settings
from app.core.exceptions import RepositoryAnalysisError
from app.core.logging import get_logger
from app.repository_analysis.models import FileIndex, IndexedFile

logger = get_logger(__name__)

#: Directories never descended into. Dependencies and build output are not
#: repository source, and including them would skew language detection.
IGNORED_DIRECTORIES: Final = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".gradle",
        ".idea",
        ".vscode",
        ".cache",
        ".turbo",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".parcel-cache",
        "dist",
        "build",
        "out",
        "coverage",
        "target",
        "vendor",
        "bower_components",
    }
)


class FileIndexer:
    """Produces a :class:`FileIndex` for a directory on disk."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._max_files = max(1, self._settings.analysis_max_files)
        self._max_depth = max(1, self._settings.analysis_max_depth)

    async def index(self, root: Path) -> FileIndex:
        """Walk ``root`` and return its file index.

        Raises:
            RepositoryAnalysisError: ``root`` is not a readable directory.
        """
        return await asyncio.to_thread(self.index_sync, root)

    def index_sync(self, root: Path) -> FileIndex:
        """Synchronous form of :meth:`index`."""
        resolved = Path(root).expanduser()
        if not resolved.is_dir():
            raise RepositoryAnalysisError(
                f"{resolved} is not a directory that can be analyzed."
            )
        resolved = resolved.resolve()

        files: list[IndexedFile] = []
        directories: set[str] = set()
        truncated = False

        for current, subdirectories, filenames in os.walk(
            resolved, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            relative_directory = _relative_posix(current_path, resolved)
            depth = (
                0 if relative_directory == "." else relative_directory.count("/") + 1
            )

            if depth >= self._max_depth:
                subdirectories[:] = []
            else:
                # In-place assignment is what prunes the walk.
                subdirectories[:] = sorted(
                    name
                    for name in subdirectories
                    if name not in IGNORED_DIRECTORIES
                    and not (current_path / name).is_symlink()
                )

            if relative_directory != ".":
                directories.add(relative_directory)

            for filename in sorted(filenames):
                if len(files) >= self._max_files:
                    truncated = True
                    subdirectories[:] = []
                    break

                file_path = current_path / filename
                if file_path.is_symlink():
                    continue

                try:
                    size = file_path.stat().st_size
                except OSError:
                    # Vanished or unreadable between listing and stat.
                    continue

                files.append(
                    IndexedFile(
                        path=_relative_posix(file_path, resolved),
                        name=filename,
                        suffix=file_path.suffix.lower(),
                        size_bytes=size,
                    )
                )

            if truncated:
                break

        index = FileIndex(
            root=resolved,
            files=tuple(files),
            paths=frozenset(file.path for file in files),
            directories=frozenset(directories),
            truncated=truncated,
        )

        logger.info(
            "Indexed repository",
            extra={
                "root": str(resolved),
                "file_count": len(index.files),
                "directory_count": len(index.directories),
                "truncated": truncated,
            },
        )
        return index


def _relative_posix(path: Path, root: Path) -> str:
    """Path relative to ``root`` using forward slashes."""
    return path.relative_to(root).as_posix()
