"""Shared filename helpers for local JSON stores."""

from __future__ import annotations

import re


def sanitize_filename(name) -> str:
    """Return a Windows-safe, bounded filename stem."""
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "unknown")).strip("_")
    return (safe or "unknown")[:150]
