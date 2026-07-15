import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    dify_base_url: str
    dify_app_api_key: str = field(repr=False)
    chatwoot_base_url: str
    chatwoot_account_id: int
    chatwoot_api_token: str = field(repr=False)
    chatwoot_webhook_secret: str = field(repr=False)
    customer_service_db_path: Path

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if environ is None else environ
        try:
            dify_base_url = source.get(
                "DIFY_BASE_URL",
                "http://127.0.0.1:8081",
            ).strip().rstrip("/")
            chatwoot_base_url = source.get(
                "CHATWOOT_BASE_URL",
                "http://127.0.0.1:3000",
            ).strip().rstrip("/")
            dify_app_api_key = source.get("DIFY_APP_API_KEY", "").strip()
            chatwoot_api_token = source.get("CHATWOOT_API_TOKEN", "").strip()
            webhook_secret = source.get("CHATWOOT_WEBHOOK_SECRET", "").strip()
            account_id = int(source.get("CHATWOOT_ACCOUNT_ID", "").strip())
            database_path = Path(
                source.get(
                    "CUSTOMER_SERVICE_DB_PATH",
                    ".runtime/customer-service/state.db",
                ).strip()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("AI customer service configuration is incomplete.") from exc

        if (
            not dify_base_url
            or not chatwoot_base_url
            or not dify_app_api_key
            or not chatwoot_api_token
            or not webhook_secret
            or account_id <= 0
            or not str(database_path)
        ):
            raise ValueError("AI customer service configuration is incomplete.")

        return cls(
            dify_base_url=dify_base_url,
            dify_app_api_key=dify_app_api_key,
            chatwoot_base_url=chatwoot_base_url,
            chatwoot_account_id=account_id,
            chatwoot_api_token=chatwoot_api_token,
            chatwoot_webhook_secret=webhook_secret,
            customer_service_db_path=database_path,
        )


@dataclass(frozen=True)
class OperationsSettings:
    api_key: str = field(repr=False)
    database_path: Path

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "OperationsSettings":
        source = os.environ if environ is None else environ
        try:
            api_key = source.get("OPERATIONS_API_KEY", "").strip()
            database_path = source.get(
                "CUSTOMER_SERVICE_DB_PATH",
                ".runtime/customer-service/state.db",
            ).strip()
        except (AttributeError, TypeError) as exc:
            raise ValueError("Operations configuration is invalid.") from exc
        if not database_path:
            raise ValueError("Operations configuration is invalid.")
        return cls(api_key=api_key, database_path=Path(database_path))
