"""Secret redaction: tokens, ``.env`` lines, high-entropy strings, and what is left alone."""

from __future__ import annotations

from janus.sandbox.redaction import REDACTED, redact, redact_bytes


def test_github_token_is_redacted() -> None:
    token = "ghp_" + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8"
    result = redact(f"cloning with {token} now")
    assert token not in result
    assert REDACTED in result


def test_aws_access_key_is_redacted() -> None:
    result = redact("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in result


def test_private_key_block_is_redacted() -> None:
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    result = redact(f"key is:\n{pem}\ndone")
    assert "MIIEowIBAAKCAQEA" not in result
    assert REDACTED in result


def test_bearer_token_is_redacted() -> None:
    result = redact("Authorization: Bearer abcdef1234567890abcdef")
    assert "abcdef1234567890abcdef" not in result
    assert "Bearer" in result


def test_env_line_value_is_redacted() -> None:
    result = redact("GITHUB_WEBHOOK_SECRET=s3cret-value")
    assert result == f"GITHUB_WEBHOOK_SECRET={REDACTED}"


def test_json_style_secret_is_redacted() -> None:
    result = redact('{"password": "hunter2"}')
    assert "hunter2" not in result
    assert REDACTED in result


def test_json_style_secret_without_space_is_redacted() -> None:
    result = redact('{"api_key":"abc123def456"}')
    assert "abc123def456" not in result
    assert REDACTED in result


def test_json_style_token_is_redacted() -> None:
    result = redact('{"token": "s3cr3t-value"}')
    assert "s3cr3t-value" not in result


def test_jwt_is_redacted() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop"
    assert jwt not in redact(f"cookie={jwt}")


def test_high_entropy_string_is_redacted() -> None:
    blob = "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6"
    assert blob not in redact(f"nonce {blob} end")


def test_ordinary_text_is_untouched() -> None:
    text = "2 tests failed in tests/test_app.py after 1.5s"
    assert redact(text) == text


def test_long_identifier_is_not_redacted() -> None:
    text = "get_or_create_run_with_defaults"
    assert redact(text) == text


def test_file_path_is_not_redacted() -> None:
    text = "janus/sandbox/runner.py"
    assert redact(text) == text


def test_versioned_absolute_path_is_not_redacted() -> None:
    text = "/usr/lib/python3.12/site-packages/pytest/__init__.py"
    assert redact(text) == text


def test_traceback_path_is_not_redacted() -> None:
    text = 'File "/usr/lib/python3.12/site-packages/foo/bar.py", line 12'
    assert redact(text) == text


def test_empty_input_is_returned_as_is() -> None:
    assert redact("") == ""


def test_redact_bytes_decodes_and_redacts() -> None:
    result = redact_bytes(b"token: ghp_" + b"a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8")
    assert b"ghp_" not in result.encode()
