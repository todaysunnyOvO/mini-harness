from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import yaml

from .profiles import normalize_profile_id


_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_FRONTMATTER_READ_LIMIT = 16_384
_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "__pycache__",
        "node_modules",
    }
)


class SkillError(ValueError):
    """Base class for safe Skill discovery and loading failures."""


class SkillNotFoundError(SkillError):
    """Raised when a requested Skill is not present in the startup index."""


class SkillTooLargeError(SkillError):
    """Raised when a Skill cannot be loaded completely within its limit."""


@dataclass(frozen=True)
class SkillIndexEntry:
    """Metadata-only startup index entry; intentionally has no content field."""

    name: str
    description: str
    source: str
    skill_dir: Path
    skill_path: Path
    trusted_root: Path
    size_bytes: int
    mtime_ns: int


@dataclass(frozen=True)
class SkillConflict:
    name: str
    selected_source: str
    selected_path: Path
    shadowed_source: str
    shadowed_path: Path


@dataclass(frozen=True)
class SkillDiagnostic:
    source: str
    path: Path
    error: str


@dataclass(frozen=True)
class LoadedSkill:
    name: str
    description: str
    source: str
    skill_dir: Path
    content: str
    revision: str


@dataclass(frozen=True)
class SkillInvocation:
    name: str
    source: str
    reference: str
    content_chars: int
    revision: str
    message: str

    @property
    def event_details(self) -> dict[str, object]:
        return {
            "name": self.name,
            "source": self.source,
            "reference": self.reference,
            "content_chars": self.content_chars,
            "revision": self.revision,
        }


class SkillCatalog:
    """Discover Skill metadata once, then load one selected SKILL.md in full.

    Source precedence is external (Profile-local first, then configured
    external directories), bundled, and finally explicitly enabled optional
    Skills. The first entry for a name wins and every later collision is
    retained as a diagnostic.
    """

    def __init__(
        self,
        *,
        profile_root: str | Path,
        profile_id: str,
        external_dirs: Sequence[str | Path],
        bundled_dir: str | Path,
        optional_dir: str | Path,
        enabled_optional: Sequence[str] = (),
        max_skill_chars: int = 40_000,
        max_index_description_chars: int = 240,
    ) -> None:
        if max_skill_chars < 256:
            raise ValueError("max_skill_chars must be at least 256")
        if max_index_description_chars < 32:
            raise ValueError(
                "max_index_description_chars must be at least 32"
            )
        normalized_profile = normalize_profile_id(profile_id)
        self._max_skill_chars = max_skill_chars
        self._max_index_description_chars = max_index_description_chars
        optional_names = frozenset(
            normalize_skill_name(name) for name in enabled_optional
        )
        profile_skills = (
            Path(profile_root).expanduser().resolve()
            / normalized_profile
            / "skills"
        )
        roots: list[tuple[str, Path]] = [("external", profile_skills)]
        roots.extend(
            ("external", Path(path).expanduser().resolve())
            for path in external_dirs
        )
        roots.append(("bundled", Path(bundled_dir).expanduser().resolve()))
        roots.append(("optional", Path(optional_dir).expanduser().resolve()))

        selected: dict[str, SkillIndexEntry] = {}
        conflicts: list[SkillConflict] = []
        diagnostics: list[SkillDiagnostic] = []
        seen_roots: set[tuple[str, Path]] = set()
        for source, root in roots:
            root_key = (source, root)
            if root_key in seen_roots:
                continue
            seen_roots.add(root_key)
            for skill_path in _iter_skill_files(root):
                try:
                    entry = self._build_entry(source, root, skill_path)
                except (OSError, UnicodeError, yaml.YAMLError, SkillError) as exc:
                    diagnostics.append(
                        SkillDiagnostic(
                            source=source,
                            path=skill_path,
                            error=str(exc),
                        )
                    )
                    continue
                if source == "optional" and entry.name not in optional_names:
                    continue
                winner = selected.get(entry.name)
                if winner is None:
                    selected[entry.name] = entry
                    continue
                conflicts.append(
                    SkillConflict(
                        name=entry.name,
                        selected_source=winner.source,
                        selected_path=winner.skill_path,
                        shadowed_source=entry.source,
                        shadowed_path=entry.skill_path,
                    )
                )

        self._entries_by_name = selected
        self._entries = tuple(
            sorted(selected.values(), key=lambda entry: entry.name)
        )
        self._conflicts = tuple(conflicts)
        self._diagnostics = tuple(diagnostics)

    @property
    def entries(self) -> tuple[SkillIndexEntry, ...]:
        return self._entries

    @property
    def conflicts(self) -> tuple[SkillConflict, ...]:
        return self._conflicts

    @property
    def diagnostics(self) -> tuple[SkillDiagnostic, ...]:
        return self._diagnostics

    def get(self, name: str) -> SkillIndexEntry:
        normalized = normalize_skill_name(name)
        entry = self._entries_by_name.get(normalized)
        if entry is None:
            raise SkillNotFoundError(
                f"Skill {normalized!r} is missing from the startup index"
            )
        return entry

    def load(self, name: str) -> LoadedSkill:
        """Load exactly one complete SKILL.md; no offset/limit API exists."""

        entry = self.get(name)
        path = entry.skill_path
        if path.is_symlink():
            raise SkillError(f"Skill file became a symbolic link: {path}")
        resolved_path = path.resolve()
        if not _is_relative_to(resolved_path, entry.trusted_root):
            raise SkillError(
                f"Skill path escaped its trusted root after indexing: {path}"
            )
        stat = resolved_path.stat()
        if stat.st_size > self._max_skill_chars * 4:
            raise SkillTooLargeError(
                f"Skill {entry.name!r} is too large to load complete"
            )
        content = resolved_path.read_text(encoding="utf-8")
        if len(content) > self._max_skill_chars:
            raise SkillTooLargeError(
                f"Skill {entry.name!r} is too large to load complete "
                f"({len(content)} > {self._max_skill_chars} characters)"
            )
        current_name, description = _read_frontmatter_metadata(
            resolved_path,
            max_description_chars=self._max_index_description_chars,
        )
        if current_name != entry.name:
            raise SkillError(
                "Skill name changed after startup; restart to rebuild the index"
            )
        return LoadedSkill(
            name=entry.name,
            description=description,
            source=entry.source,
            skill_dir=resolved_path.parent,
            content=content,
            revision=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )

    def build_invocation(
        self,
        name: str,
        user_instruction: str = "",
    ) -> SkillInvocation:
        loaded = self.load(name)
        instruction = user_instruction.strip()
        reference = _skill_reference(loaded.source, loaded.name)
        parts = [
            (
                f'[IMPORTANT: The user invoked the "{loaded.name}" skill. '
                "Follow the complete SKILL.md instructions loaded below.]"
            ),
            "",
            loaded.content.rstrip(),
            "",
            f"[Skill reference: {reference}]",
            (
                "Relative paths in these instructions are relative to this "
                "logical Skill root. Its host filesystem location is private "
                "and must not be inferred."
            ),
        ]
        if instruction:
            parts.extend(
                [
                    "",
                    "[User instruction]",
                    instruction,
                ]
            )
        message = "\n".join(parts).strip()
        return SkillInvocation(
            name=loaded.name,
            source=loaded.source,
            reference=reference,
            content_chars=len(loaded.content),
            revision=loaded.revision,
            message=message,
        )

    def _build_entry(
        self,
        source: str,
        root: Path,
        skill_path: Path,
    ) -> SkillIndexEntry:
        resolved_path = skill_path.resolve()
        if not _is_relative_to(resolved_path, root):
            raise SkillError("Skill path escapes its discovery root")
        name, description = _read_frontmatter_metadata(
            resolved_path,
            max_description_chars=self._max_index_description_chars,
        )
        stat = resolved_path.stat()
        return SkillIndexEntry(
            name=name,
            description=description,
            source=source,
            skill_dir=resolved_path.parent,
            skill_path=resolved_path,
            trusted_root=root,
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )


def normalize_skill_name(name: object) -> str:
    if not isinstance(name, str):
        raise SkillError("skill name must be a string")
    normalized = name.strip().lower()
    if not _SKILL_NAME_RE.fullmatch(normalized):
        raise SkillError(
            "skill name must match [a-z0-9][a-z0-9-]{0,63}"
        )
    return normalized


def _skill_reference(source: str, name: str) -> str:
    if source not in {"external", "bundled", "optional"}:
        raise SkillError(f"unsupported Skill source: {source}")
    return f"skill://{source}/{normalize_skill_name(name)}/"


def render_skills_index(
    base_system_prompt: str,
    entries: Iterable[SkillIndexEntry],
) -> str:
    """Append only stable Skill summaries to a new Session system prompt."""

    indexed = tuple(entries)
    if not indexed:
        return base_system_prompt
    lines = [
        base_system_prompt.rstrip(),
        "",
        "<available_skills>",
        (
            "These are metadata summaries only. Load a selected skill in full "
            "with the Skill command before following it."
        ),
    ]
    for entry in indexed:
        lines.append(
            f"- {entry.name}: {entry.description} (source={entry.source})"
        )
    lines.extend(
        [
            "</available_skills>",
            "",
            (
                "Do not assume a summary is the complete instruction. A Skill "
                "must be explicitly loaded for the current user turn."
            ),
        ]
    )
    return "\n".join(lines)


def _iter_skill_files(root: Path):
    if not root.is_dir() or root.is_symlink():
        return
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            directory
            for directory in directories
            if directory not in _EXCLUDED_DIRECTORIES
            and not (current_path / directory).is_symlink()
        )
        if "SKILL.md" not in files:
            continue
        candidate = current_path / "SKILL.md"
        if not candidate.is_symlink():
            yield candidate
        # Everything below a Skill root is support data, not another active
        # Skill (for example references/archived/SKILL.md).
        directories[:] = []


def _read_frontmatter_metadata(
    path: Path,
    *,
    max_description_chars: int,
) -> tuple[str, str]:
    with path.open("rb") as handle:
        prefix = handle.read(_FRONTMATTER_READ_LIMIT + 1)
    text = prefix.decode("utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillError("SKILL.md must start with YAML frontmatter")
    closing_index = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        ),
        None,
    )
    if closing_index is None:
        raise SkillError(
            "SKILL.md frontmatter must close within the first "
            f"{_FRONTMATTER_READ_LIMIT} bytes"
        )
    frontmatter = yaml.safe_load("\n".join(lines[1:closing_index])) or {}
    if not isinstance(frontmatter, dict):
        raise SkillError("SKILL.md frontmatter must be a YAML mapping")
    name = normalize_skill_name(frontmatter.get("name"))
    raw_description = frontmatter.get("description")
    if not isinstance(raw_description, str) or not raw_description.strip():
        raise SkillError(
            f"Skill {name!r} must provide a non-empty description"
        )
    description = " ".join(raw_description.split())
    if len(description) > max_description_chars:
        description = description[: max_description_chars - 3].rstrip() + "..."
    return name, description


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
