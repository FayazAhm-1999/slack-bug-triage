"""Application settings loaded from environment variables.

Fails at import time if any required variable is missing — intentional
fail-fast behaviour so misconfiguration is obvious immediately.
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    slack_bot_token: str
    slack_signing_secret: str
    anthropic_api_key: str
    github_token: str
    github_repo: str                # "owner/repo"
    # Stored as a raw comma-separated string to avoid pydantic-settings 2.x
    # attempting to JSON-decode a list field from the env (which fails for
    # plain "U123,U456" values).  Parsed once at startup into authorized_user_ids.
    authorized_slack_users: str
    bug_channel_id: str
    duplicate_threshold: float = Field(default=0.85, ge=0.0, le=1.0)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    @model_validator(mode="after")
    def _validate_github_repo(self) -> "Settings":
        parts = self.github_repo.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(
                f"GITHUB_REPO must be in 'owner/repo' format, got: {self.github_repo!r}"
            )
        return self

    # Parsed once at startup; use this everywhere instead of the raw string.
    @property
    def authorized_user_ids(self) -> list[str]:
        return [u.strip() for u in self.authorized_slack_users.split(",") if u.strip()]

    @property
    def github_owner(self) -> str:
        return self.github_repo.split("/")[0]

    @property
    def github_repo_name(self) -> str:
        return self.github_repo.split("/")[1]


# Module-level singleton — imported by all other modules.
settings = Settings()

# Cache authorized_user_ids once at startup rather than re-parsing on every request.
AUTHORIZED_USER_IDS: frozenset[str] = frozenset(settings.authorized_user_ids)
