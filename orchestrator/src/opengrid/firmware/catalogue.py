"""The firmware catalogue (R3.1): the only versions a campaign may target, per hardware revision, each with
its image sha256 and release note. Built from `[firmware.catalogue]` config entries plus `og.firmware_catalogue`
rows (a table row wins over a config entry with the same key). Invalid entries are dropped with a warning,
never "fixed": a bad hash must not become a signable image.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from opengrid.firmware.model import (
    CatalogueEntry,
    is_downgrade,
    is_valid_sha256,
    is_valid_version,
    version_key,
)

logger = logging.getLogger(__name__)


def entry_from_mapping(raw: Mapping[str, Any], *, source: str = "config") -> CatalogueEntry | None:
    """One validated entry, or None (logged) when the version, sha256 or revision is malformed."""
    version = str(raw.get("version", "")).strip()
    revision = str(raw.get("hardware_revision", "")).strip()
    sha256 = str(raw.get("sha256", "")).strip().lower()
    note = str(raw.get("release_note", "")).strip()
    if not is_valid_version(version) or not revision or not is_valid_sha256(sha256) or not note:
        logger.warning(
            "dropping invalid firmware catalogue entry",
            extra={"version": version, "hardware_revision": revision, "source": source},
        )
        return None
    released = raw.get("released_at")
    return CatalogueEntry(
        version=version,
        hardware_revision=revision,
        sha256=sha256,
        release_note=note,
        released_at=str(released) if released is not None else None,
        source="table" if source == "table" else "config",
    )


@dataclass(frozen=True, slots=True)
class Catalogue:
    entries: tuple[CatalogueEntry, ...]

    @classmethod
    def build(
        cls, config_entries: Iterable[Mapping[str, Any]], table_entries: Iterable[Mapping[str, Any]] = ()
    ) -> Catalogue:
        by_key: dict[tuple[str, str], CatalogueEntry] = {}
        for raw, source in [(e, "config") for e in config_entries] + [(e, "table") for e in table_entries]:
            entry = entry_from_mapping(raw, source=source)
            if entry is not None:
                by_key[(entry.version, entry.hardware_revision)] = entry
        ordered = sorted(by_key.values(), key=lambda e: (e.hardware_revision, version_key(e.version)))
        return cls(tuple(ordered))

    def lookup(self, version: str, hardware_revision: str | None) -> CatalogueEntry | None:
        if hardware_revision is None:
            return None
        for entry in self.entries:
            if entry.version == version and entry.hardware_revision == hardware_revision:
                return entry
        return None

    def versions(self) -> list[str]:
        return sorted({e.version for e in self.entries}, key=version_key)

    def has_version(self, version: str) -> bool:
        return any(e.version == version for e in self.entries)

    def as_json(self) -> list[dict[str, Any]]:
        return [
            {
                "version": e.version,
                "hardware_revision": e.hardware_revision,
                "sha256": e.sha256,
                "release_note": e.release_note,
                "released_at": e.released_at,
                "source": e.source,
            }
            for e in self.entries
        ]


def hub_target_problem(
    catalogue: Catalogue,
    *,
    target_version: str,
    hardware_revision: str | None,
    current_version: str | None,
    allow_downgrade: bool,
) -> str | None:
    """Why `target_version` may not be installed on this hub (a reason code), or None when it may.
    Shared by campaign creation (hubs that fail are SKIPPED) and the guardian check (refuse to sign)."""
    if hardware_revision is None:
        return "FIRMWARE_HARDWARE_REVISION_UNKNOWN"
    if catalogue.lookup(target_version, hardware_revision) is None:
        return "FIRMWARE_NOT_IN_CATALOGUE_FOR_HARDWARE"
    if current_version == target_version:
        return "FIRMWARE_ALREADY_AT_TARGET"
    if is_downgrade(current_version, target_version) and not allow_downgrade:
        return "FIRMWARE_DOWNGRADE_NOT_ALLOWED"
    return None
