from opsmith.redaction import Redactor, redact_text
from tests.conftest import FAKE_AWS_KEY


def test_redacts_common_pii_and_secrets() -> None:
    text = (
        "user jane.doe@example.com from 10.1.2.3 paid with 4111 1111 1111 1111 "
        f"key={FAKE_AWS_KEY} token ghp_" + "a" * 36 + " ssn 123-45-6789"
    )
    out, counts = Redactor().redact_text(text)
    for leaked in (
        "jane.doe@example.com",
        "10.1.2.3",
        "4111 1111 1111 1111",
        FAKE_AWS_KEY,
        "ghp_",
        "123-45-6789",
    ):
        assert leaked not in out
    assert counts["email"] == 1
    assert counts["aws_access_key"] == 1
    assert counts["credit_card"] == 1


def test_luhn_guard_keeps_non_card_numbers() -> None:
    out = redact_text("order 1234567890123 shipped")
    assert "1234567890123" in out


def test_keeps_assignment_key_masks_value() -> None:
    out = redact_text('password="hunter22hunter" and api_key: s3cr3tvalue99')
    assert "hunter22hunter" not in out
    assert "s3cr3tvalue99" not in out
    assert "password=" in out


def test_url_credentials_and_jwt() -> None:
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )
    out = redact_text(f"postgres://app:supersecret@db:5432/x Authorization: Bearer {jwt}")
    assert "supersecret" not in out
    assert jwt not in out
    assert "postgres://app:" in out


def test_private_key_block() -> None:
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----"
    assert "MIIEow" not in redact_text(f"leaked: {pem}")


def test_recursive_and_idempotent() -> None:
    redactor = Redactor(extra_patterns=[r"cust-\d{6}"])
    data = {"msg": "mail a@b.io", "nested": [{"id": "cust-123456"}], "n": 3}
    once = redactor.redact(data)
    assert once["nested"][0]["id"] == "[REDACTED:custom_0]"
    assert once["n"] == 3
    assert redactor.redact(once) == once
