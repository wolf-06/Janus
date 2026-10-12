"""Log-archive decoding is total: it must never raise on untrusted input."""

from __future__ import annotations

import io
import zipfile

from janus.github.logs import extract_log_text, looks_like_zip


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as bundle:
        for name, payload in members.items():
            bundle.writestr(name, payload)
    return buffer.getvalue()


def test_extracts_every_member_in_name_order() -> None:
    archive = _zip_bytes(
        {
            "2_second.log": b"second job output\n",
            "1_first.log": b"first job output\n",
        }
    )

    text = extract_log_text(archive)

    assert "first job output" in text
    assert "second job output" in text
    assert text.index("1_first.log") < text.index("2_second.log")
    assert "===== 1_first.log =====" in text
    assert "===== 2_second.log =====" in text


def test_plain_text_payload_passes_through() -> None:
    payload = b"not a zip, just a log line\n"
    assert not looks_like_zip(payload)
    assert extract_log_text(payload) == "not a zip, just a log line\n"


def test_empty_input_returns_empty_string() -> None:
    assert extract_log_text(b"") == ""


def test_truncated_zip_does_not_raise() -> None:
    archive = _zip_bytes({"1_job.log": b"hello world\n"})
    truncated = archive[: len(archive) // 2]

    text = extract_log_text(truncated)

    assert isinstance(text, str)


def test_corrupt_zip_does_not_raise() -> None:
    corrupt = b"PK\x03\x04" + b"\x00" * 32

    text = extract_log_text(corrupt)

    assert isinstance(text, str)


def test_invalid_utf8_does_not_raise() -> None:
    payload = b"\xff\xfe\x00\x80 broken \xc3\x28"
    text = extract_log_text(payload)
    assert isinstance(text, str)
    assert "broken" in text


def test_invalid_utf8_inside_zip_does_not_raise() -> None:
    archive = _zip_bytes({"1_job.log": b"\xff\xfe raw \x80 bytes"})
    text = extract_log_text(archive)
    assert isinstance(text, str)
    assert "===== 1_job.log =====" in text
