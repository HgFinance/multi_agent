from __future__ import annotations

import os
from unittest import TestCase, mock

from apps.api.discord_read import (
    DEPARTMENT_CHANNEL_ENV,
    _channel_config,
    credentials,
)


class DiscordReadChannelConfigTest(TestCase):
    def test_qa_uses_established_feedback_channel_variable(self) -> None:
        self.assertEqual(DEPARTMENT_CHANNEL_ENV["qa"], "QA_DISCORD_CHANNEL_ID")
        with mock.patch.dict(
            os.environ,
            {
                "QA_DISCORD_CHANNEL_ID": "qa-channel",
                "DISCORD_CEO_CHANNEL_ID": "ceo-channel",
            },
            clear=False,
        ):
            self.assertEqual(_channel_config("qa"), ("qa-channel", "department"))

    def test_qa_missing_channel_falls_back_to_shared_ceo(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "QA_DISCORD_CHANNEL_ID": "",
                "DISCORD_CEO_CHANNEL_ID": "ceo-channel",
            },
            clear=False,
        ):
            self.assertEqual(_channel_config("qa"), ("ceo-channel", "shared_ceo"))

    def test_credentials_keeps_qa_channel_and_token_together(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "DISCORD_BOT_TOKEN_QA": "qa-token",
                "QA_DISCORD_CHANNEL_ID": "qa-channel",
            },
            clear=False,
        ):
            self.assertEqual(credentials("qa"), ("qa-token", "qa-channel"))
