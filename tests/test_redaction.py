from collections import Counter

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


def _leaks(text: str, *secrets: str) -> list[str]:
    out = redact_text(text)
    return [s for s in secrets if s in out]


def test_card_number_next_to_other_digits() -> None:
    card = "4111 1111 1111 1111"
    solid = "4111111111111111"
    for text in (
        f"paid with {card} 123 ok",  # CVV
        f"card {solid} 12/27",  # expiry
        f"charged card {solid} 3 times",
        f"order 12 {solid}",
        f"order 12 {card} 123",
        "amex 3782 822463 10005 exp 11",
    ):
        out, counts = Redactor().redact_text(text)
        assert counts["credit_card"] == 1, text
        assert not [n for n in (card, solid, "3782 822463 10005") if n in out], text
    # What stood next to the card is kept, and only the card is replaced.
    assert redact_text(f"order 12 {card} 123") == "order 12 [REDACTED:credit_card] 123"
    # Long ids and number lists that are not card-shaped stay readable.
    for text in ("trace 12345678901234567890123", "buckets 1 2 3 4 5 6 7 8 9 10 11 12 13 14"):
        assert redact_text(text) == text


def test_credentials_in_json_and_prefixed_keys() -> None:
    secret = "hunter22hunter"
    aws = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    cases = [
        f'request body: {{"username":"bob","password":"{secret}"}}',
        f'{{"api_key": "{secret}"}}',
        f"{{'apiKey': '{secret}'}}",
        f"DB_PASSWORD={secret}",
        f"db_password={secret}",
        f"MYSQL_ROOT_PASSWORD: {secret}",
        f"token={secret}",
        f"refresh_token={secret}&grant_type=x",
        f"passphrase={secret}",
        f"clientSecret = {secret}",
        f"X-Auth-Token: {secret}",
    ]
    for text in cases:
        assert not _leaks(text, secret), text
    assert not _leaks(f"AWS_SECRET_ACCESS_KEY={aws}", aws)
    assert not _leaks(f"aws_secret_access_key = {aws}", aws)
    assert redact_text(f"DB_PASSWORD={secret}") == "DB_PASSWORD=[REDACTED:credential_assignment]"
    # A quoted value is masked whole, not just its first word.
    assert not _leaks('password="correct horse battery"', "correct", "horse", "battery")
    assert "grant_type=x" in redact_text(f"refresh_token={secret}&grant_type=x")
    # Ordinary text that merely mentions the words is left alone.
    for text in ("max_tokens=400000", "token_count: 123456", "passwordless login enabled"):
        assert redact_text(text) == text


def test_basic_authorization_header() -> None:
    b64 = "dXNlcjpodW50ZXIyMmh1bnRlcg=="
    for text in (
        f"Authorization: Basic {b64}",
        f"proxy-authorization: basic {b64}",
        f"{{'Authorization': 'Basic {b64}'}}",
    ):
        assert not _leaks(text, b64), text
    assert redact_text("basic authentication failed") == "basic authentication failed"


def test_url_credentials_edge_cases() -> None:
    assert not _leaks("connecting to redis://:sup3rs3cret@cache:6379/0", "sup3rs3cret")
    assert not _leaks("postgres://app:p@ssw0rd@db:5432/x", "ssw0rd", "p@ss")
    assert not _leaks("postgres://app:pa/ssw0rd@db:5432/x", "ssw0rd")
    assert not _leaks("smtp://jane@example.com:mailpass99@mail:587", "mailpass99")
    out = redact_text("postgres://app:p@ssw0rd@db:5432/x")
    assert out == "postgres://app:[REDACTED:url_credentials]@db:5432/x"
    # URLs without credentials keep their host, port and path.
    for text in (
        "GET http://api.internal:8080/users/a@b.example",
        "see https://example.com/a/b?x=1",
        "ssh://git@github.com:org/repo.git",
    ):
        assert "[REDACTED:url_credentials]" not in redact_text(text), text


def test_structured_fields_named_like_credentials() -> None:
    redactor = Redactor()
    data = {
        "password": "hunter22hunter",
        "db": {"API_KEY": "s3cr3t", "port": 5432},
        "Authorization": "Basic dXNlcjpwYXNz",
        "set-cookie": "sid=abc",
        "refreshToken": ["tok-1", "tok-2"],
        "pin_token": 123456,
        "card_number": 4111111111111111,
        "order_id": 1234567890123,
        "count": 3,
        "enabled": True,
        "password_hint": "",
        "message": "plain text",
    }
    counts: Counter[str] = Counter()
    out = redactor.redact(data, counts)
    token = "[REDACTED:credential_assignment]"
    assert out["password"] == token
    assert out["db"] == {"API_KEY": token, "port": 5432}
    assert out["Authorization"] == token
    assert out["set-cookie"] == token
    assert out["refreshToken"] == [token, token]
    assert out["pin_token"] == token
    assert out["card_number"] == "[REDACTED:credit_card]"
    assert out["order_id"] == 1234567890123  # not a card number
    assert (out["count"], out["enabled"], out["message"]) == (3, True, "plain text")
    assert counts["credential_assignment"] == 7
    assert counts["credit_card"] == 1
    assert redactor.redact(out) == out  # idempotent
    # A bare string has no field name, so only its content decides.
    assert redactor.redact("hunter22hunter") == "hunter22hunter"


def test_everything_under_a_credential_named_field_is_masked() -> None:
    token = "[REDACTED:credential_assignment]"
    out = Redactor().redact(
        {
            "password": {"old": "hunter2xx", "new": "Tr0ub4dor&3"},
            "authorization": {"scheme": "Basic", "credentials": ["dXNlcjpwYXNz"]},
            "request": {"headers": {"cookie": {"sid": "abc123"}}, "path": "/pay"},
        }
    )
    assert out["password"] == {"old": token, "new": token}
    assert out["authorization"] == {"scheme": token, "credentials": [token]}
    assert out["request"] == {"headers": {"cookie": {"sid": token}}, "path": "/pay"}


def test_builtin_patterns_stay_linear_on_long_runs() -> None:
    # Each of these took seconds to minutes per 40 kB with the unpinned patterns (time grew
    # with the square of the length). No timing assertion: a regression shows as a hang.
    units = ("a.", "a-", "1.", "a+", "a@", "x://a:", "password ", "1 ", "1111 ")
    units += ("eyJaaaaaaaa-", "eyJaaaaaaaa.eyJ", "-----BEGIN PRIVATE KEY-----")
    for unit in units:
        text = unit * (200_000 // len(unit))
        assert isinstance(redact_text(text), str)


def test_private_key_cut_off_by_truncation_is_still_masked() -> None:
    out = redact_text("dump: -----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq...[truncated")
    assert "MIIEvQIBADANBgkq" not in out
    begin, end = "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----"
    two = f"{begin} then {begin}\nAAAA\n{end}"
    assert "AAAA" not in redact_text(two)


def test_jwt_inside_a_hyphenated_run() -> None:
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )
    assert jwt not in redact_text(f"session-{jwt}")
