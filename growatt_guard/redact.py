from __future__ import annotations

from typing import Any


def mask_identifier(value: Any) -> str:
    """Keep an identifier recognisable in logs without publishing it.

    Plant ids and device serials are account-identifying, so the log keeps only
    the last four characters: enough to tell two devices apart in a log, not
    enough to quote elsewhere.
    """
    text = str(value or "").strip()
    if not text:
        return "(none)"
    if len(text) <= 4:
        return "****"
    return "****" + text[-4:]
