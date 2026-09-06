import logging

from app.channels.discord import PersonalInboxDiscordClient
from app.core.config import get_settings
from app.db import get_session_factory


def main() -> None:
    settings = get_settings()
    if settings.discord_bot_token is None or not settings.discord_bot_token.get_secret_value():
        raise RuntimeError("DISCORD_BOT_TOKEN is not configured")
    if settings.discord_allowed_user_id is None:
        raise RuntimeError("DISCORD_ALLOWED_USER_ID is not configured")

    logging.basicConfig(level=settings.log_level)
    client = PersonalInboxDiscordClient(settings, get_session_factory())
    client.run(settings.discord_bot_token.get_secret_value())


if __name__ == "__main__":
    main()
