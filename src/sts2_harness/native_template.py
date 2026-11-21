"""Verify and retain one immutable native template, then copy fresh writable profiles."""

import hashlib
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from sts2_harness.client.models import WireModel

type Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class TemplateError(Exception):
    """The native template could not be verified without touching a player profile."""


class TemplateFile(WireModel):
    path: str
    sha256: Sha256


class TemplateManifest(WireModel):
    files: tuple[TemplateFile, ...]
    game_assembly_sha256: Sha256
    generator_sha256: Sha256
    epochs: Annotated[int, Field(gt=0)]
    cards: Annotated[int, Field(gt=0)]
    relics: Annotated[int, Field(gt=0)]
    potions: Annotated[int, Field(gt=0)]
    characters: Annotated[tuple[str, ...], Field(min_length=5)]
    ascension_total: Annotated[int, Field(ge=50)]
    runs: Annotated[int, Field(ge=0, le=1)]
    bosses_seen: tuple[str, ...] = ()
    history: Literal[0]
    has_run: Literal[False]
    has_multiplayer_run: Literal[False]
    tutorials_seen: Literal[True]
    ascension_ftue_seen: Literal[True]


@dataclass(frozen=True)
class TemplateEvidence:
    manifest_sha256: str
    settings_sha256: str
    game_assembly_sha256: str


_FILES: frozenset[str] = frozenset(
    (
        "default/1/settings.save",
        "default/1/modded/profile1/saves/progress.save",
        "default/1/modded/profile1/saves/prefs.save",
    )
)


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


class NativeTemplate:
    def __init__(self, manifest_path: Path) -> None:
        try:
            if not manifest_path.name.endswith(".manifest.json"):
                raise TemplateError("A native template requires its .manifest.json file.")
            self._manifest_bytes = manifest_path.read_bytes()
            self._manifest = TemplateManifest.model_validate_json(self._manifest_bytes)
            # Historical manifests can still be read for archive verification.
            if self._manifest.runs != 1 or not self._manifest.bosses_seen:
                raise TemplateError("Regenerate the native template with the current generator.")
            self._source = manifest_path.with_name(
                manifest_path.name.removesuffix(".manifest.json")
            )
            if len(set(self._manifest.characters)) != len(self._manifest.characters):
                raise TemplateError("The native template character metadata contains duplicates.")
            files = self._manifest.files
            if len(files) != 3 or frozenset(item.path for item in files) != _FILES:
                raise TemplateError("The template must contain exactly the three native files.")
            for item in files:
                path = self._source / item.path
                if (
                    path.is_symlink()
                    or path.resolve().is_relative_to(self._source.resolve()) is False
                    or path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
                    or digest(path) != item.sha256
                ):
                    raise TemplateError("Native template file verification failed.")
            self.evidence = TemplateEvidence(
                hashlib.sha256(self._manifest_bytes).hexdigest(),
                next(item.sha256 for item in files if item.path == "default/1/settings.save"),
                self._manifest.game_assembly_sha256,
            )
        except ValidationError as error:
            raise TemplateError(
                "Invalid native template manifest. Regenerate it with the current generator."
            ) from error
        except OSError as error:
            raise TemplateError("Cannot read the verified native template.") from error

    def copy_to(self, user_directory: Path, archive_directory: Path) -> TemplateEvidence:
        # Exclusive directory creation prevents reusing a previous run's mutated profile.
        user_directory.mkdir(parents=True)
        for item in self._manifest.files:
            source = self._source / item.path
            if digest(source) != item.sha256:
                raise TemplateError("The native template changed before copying.")
            destination = user_directory / item.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            if digest(destination) != item.sha256:
                raise TemplateError("The native template changed while copying.")
            destination.chmod(0o600)
        self._retain(archive_directory)
        return self.evidence

    def _retain(self, archive_directory: Path) -> None:
        archive_directory.mkdir(parents=True, exist_ok=True)
        retained = archive_directory / self.evidence.manifest_sha256
        if retained.exists():
            self._verify_retained(retained)
            return
        with tempfile.TemporaryDirectory(prefix=".template-", dir=archive_directory) as temporary:
            staged = Path(temporary) / "content"
            staged.mkdir()
            for item in self._manifest.files:
                destination = staged / item.path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self._source / item.path, destination)
                destination.chmod(0o444)
            (staged / "manifest.json").write_bytes(self._manifest_bytes)
            (staged / "manifest.json").chmod(0o444)
            try:
                staged.rename(retained)
            except OSError:
                if not retained.is_dir():
                    raise
                self._verify_retained(retained)

    def _verify_retained(self, retained: Path) -> None:
        if digest(retained / "manifest.json") != self.evidence.manifest_sha256 or any(
            digest(retained / item.path) != item.sha256 for item in self._manifest.files
        ):
            raise TemplateError("The retained template content does not match its digest.")
