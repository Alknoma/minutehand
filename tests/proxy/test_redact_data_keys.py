"""A provider whose wire holds its own data under a credential's name (`Manifest.data_keys`: a reCAPTCHA response
token, an FCM registration token) has its calls kept with that data as sent; every other credential key in the same
body is still redacted, and a provider that declares none is redacted as before."""

from __future__ import annotations

import json

from minutehand.adapters.proxy import redact


def test_a_data_key_is_kept_as_sent_and_the_credentials_beside_it_are_redacted() -> None:
    sent = b'{"event": {"token": "03AFcWeA-human", "siteKey": "6Lc"}, "access_token": "ya29.secret"}'
    kept, _ = redact.kept(sent, "application/json", data=frozenset({"token"}))
    assert kept is not None
    assert json.loads(kept) == {"event": {"token": "03AFcWeA-human", "siteKey": "6Lc"}, "access_token": "[redacted]"}
    assert kept == sent.decode().replace('"ya29.secret"', '"[redacted]"'), "every other byte as it was sent"


def test_without_data_keys_a_token_field_is_still_redacted() -> None:
    kept, _ = redact.kept(b'{"message": {"token": "fGcm-phone"}}', "application/json")
    assert kept is not None and json.loads(kept) == {"message": {"token": "[redacted]"}}


def test_a_data_key_matches_its_name_in_any_case_and_nothing_else() -> None:
    kept, _ = redact.kept(b'{"Token": "t", "refresh_token": "r"}', "application/json", data=frozenset({"token"}))
    assert kept is not None and json.loads(kept) == {"Token": "t", "refresh_token": "[redacted]"}
