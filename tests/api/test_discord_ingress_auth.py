from __future__ import annotations

import os
from unittest.mock import patch

from apps.api.discord_ingress_auth import bearer_is_authorized, configured_secret


def test_private_discord_ingress_bearer_is_exact_and_case_safe() -> None:
    secret = "discord-ingress-private-key-0123456789abcdef"
    with patch.dict(os.environ, {"CEO_DISCORD_INGRESS_API_KEY": secret}):
        assert configured_secret() == secret
        assert bearer_is_authorized(f"Bearer {secret}") is True
        assert bearer_is_authorized(f"bearer {secret}") is True
        assert bearer_is_authorized(f"Bearer {secret}x") is False
        assert bearer_is_authorized(secret) is False


def test_missing_short_placeholder_or_control_secret_is_never_authority() -> None:
    for value in (
        "",
        "short",
        "x" * 40,
        "replace_me_with_a_real_discord_ingress_key",
        "valid-looking-key-with-a-control\ncharacter-012345",
    ):
        with patch.dict(os.environ, {"CEO_DISCORD_INGRESS_API_KEY": value}):
            assert configured_secret() is None
            assert bearer_is_authorized(f"Bearer {value}") is False
