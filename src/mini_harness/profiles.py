from __future__ import annotations

import re


_PROFILE_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,63})$")


def normalize_profile_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("profile_id must be a non-empty string")
    profile_id = value.strip()
    if not _PROFILE_ID.fullmatch(profile_id):
        raise ValueError(
            "profile_id must be 1-64 ASCII letters, digits, underscores, "
            "or hyphens, and must start with a letter or digit"
        )
    return profile_id
