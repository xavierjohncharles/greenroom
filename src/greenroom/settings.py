"""Runtime settings, read from the environment exactly once.

Nothing in here holds a credential value. The Gmail/Calendar OAuth refresh token
lives in Secret Manager and is fetched at call time by `tools.secrets`; the only
thing this module knows is the *name* of the secret.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Google Cloud -------------------------------------------------------
    google_cloud_project: str = Field(default="", alias="GOOGLE_CLOUD_PROJECT")
    google_cloud_location: str = Field(default="europe-west2", alias="GOOGLE_CLOUD_LOCATION")
    google_genai_use_vertexai: bool = Field(default=True, alias="GOOGLE_GENAI_USE_VERTEXAI")

    # --- Safety -------------------------------------------------------------
    # When true, every outbound side effect is logged and never performed.
    # Defaults to TRUE so that forgetting to set it can never send real mail.
    dry_run: bool = Field(default=True, alias="GREENROOM_DRY_RUN")

    # --- Identity -----------------------------------------------------------
    agent_mailbox: str = Field(default="admin@beatidapp.com", alias="GREENROOM_MAILBOX")

    # --- Firestore ----------------------------------------------------------
    firestore_database: str = Field(default="(default)", alias="GREENROOM_FIRESTORE_DB")

    # --- Secret Manager (names only, never values) --------------------------
    oauth_token_secret: str = Field(
        default="greenroom-oauth-refresh-token", alias="GREENROOM_OAUTH_SECRET"
    )
    oauth_client_secret: str = Field(
        default="greenroom-oauth-client", alias="GREENROOM_OAUTH_CLIENT_SECRET"
    )

    # --- Dashboard demo gate ------------------------------------------------
    dashboard_secret: str = Field(default="", alias="GREENROOM_DASHBOARD_SECRET")

    # --- Pub/Sub push verification -----------------------------------------
    push_sa_email: str = Field(default="", alias="GREENROOM_PUSH_SA_EMAIL")
    push_audience: str = Field(default="", alias="GREENROOM_PUSH_AUDIENCE")
    pubsub_topic: str = Field(default="greenroom-gmail", alias="GREENROOM_PUBSUB_TOPIC")

    # --- Storage ------------------------------------------------------------
    poster_bucket: str = Field(default="", alias="GREENROOM_POSTER_BUCKET")

    # --- Gmail labels -------------------------------------------------------
    label_root: str = Field(default="greenroom", alias="GREENROOM_LABEL")

    @property
    def label_escalated(self) -> str:
        return f"{self.label_root}/escalated"

    @property
    def label_quarantine(self) -> str:
        return f"{self.label_root}/quarantine"

    @property
    def is_configured(self) -> bool:
        """True once we have enough to talk to Google Cloud at all."""
        return bool(self.google_cloud_project)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. Cached so config is read once per container."""
    return Settings()
