"""Decode GitHub workflow-run log archives into plain text.

``GitHubClient.download_run_logs`` returns raw bytes: GitHub answers with a ZIP archive
containing one log file per job. This module turns that archive into a single string so
downstream triage can read the log without caring about the container format.

The input is untrusted (it is fetched from a remote service and may be truncated,
corrupt, or not a ZIP at all), so every entry point here is total: it never raises and
degrades to a best-effort UTF-8 decode instead of failing the caller.
"""

from __future__ import annotations

import io
import zipfile

_ZIP_MAGIC = b"PK\x03\x04"


def looks_like_zip(data: bytes) -> bool:
    """Return ``True`` when ``data`` starts with the local-file ZIP signature."""
    return data.startswith(_ZIP_MAGIC)


def _member_separator(name: str) -> str:
    return f"\n\n===== {name} =====\n\n"


def extract_log_text(archive: bytes) -> str:
    """Return the combined text of every member of a GitHub log archive.

    When ``archive`` looks like a ZIP it is opened and each member is decoded as UTF-8
    (``errors="replace"``) and joined with a ``===== <name> =====`` separator, sorted by
    member name so the output is deterministic. Otherwise the bytes are decoded directly.

    This function is intentionally total: the log is untrusted input and a parse failure
    must degrade to a best-effort decode rather than raise.
    """
    if not archive:
        return ""

    if looks_like_zip(archive):
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                names = sorted(bundle.namelist())
                parts = [
                    _member_separator(name) + bundle.read(name).decode("utf-8", errors="replace")
                    for name in names
                ]
            return "".join(parts)
        except Exception:
            # Corrupt/truncated archive: fall through to a raw decode.
            pass

    return archive.decode("utf-8", errors="replace")
