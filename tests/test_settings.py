import unittest
from pathlib import Path

from app.settings import OperationsSettings, Settings


def valid_environment() -> dict[str, str]:
    return {
        "DIFY_BASE_URL": " http://127.0.0.1:8081 ",
        "DIFY_APP_API_KEY": " app-secret ",
        "CHATWOOT_BASE_URL": " http://127.0.0.1:3000/ ",
        "CHATWOOT_ACCOUNT_ID": " 1 ",
        "CHATWOOT_API_TOKEN": " chatwoot-secret ",
        "CHATWOOT_WEBHOOK_SECRET": " webhook-secret ",
        "CUSTOMER_SERVICE_DB_PATH": " .runtime/customer-service/state.db ",
    }


class SettingsTests(unittest.TestCase):
    def test_from_env_normalizes_and_validates_values(self) -> None:
        settings = Settings.from_env(valid_environment())

        self.assertEqual(settings.dify_base_url, "http://127.0.0.1:8081")
        self.assertEqual(settings.chatwoot_base_url, "http://127.0.0.1:3000")
        self.assertEqual(settings.chatwoot_account_id, 1)
        self.assertEqual(
            settings.customer_service_db_path,
            Path(".runtime/customer-service/state.db"),
        )

    def test_missing_or_invalid_values_raise_generic_error(self) -> None:
        environment = valid_environment()
        environment["CHATWOOT_API_TOKEN"] = "sensitive-missing-token"
        environment["DIFY_APP_API_KEY"] = ""

        with self.assertRaisesRegex(
            ValueError,
            "AI customer service configuration is incomplete",
        ) as raised:
            Settings.from_env(environment)

        self.assertNotIn("sensitive-missing-token", str(raised.exception))

        environment = valid_environment()
        environment["CHATWOOT_ACCOUNT_ID"] = "not-a-number"
        with self.assertRaisesRegex(
            ValueError,
            "AI customer service configuration is incomplete",
        ):
            Settings.from_env(environment)

    def test_repr_does_not_expose_secrets(self) -> None:
        settings = Settings.from_env(valid_environment())

        rendered = repr(settings)
        self.assertNotIn("app-secret", rendered)
        self.assertNotIn("chatwoot-secret", rendered)
        self.assertNotIn("webhook-secret", rendered)


class OperationsSettingsTests(unittest.TestCase):
    def test_operations_settings_are_independent_from_customer_service_secrets(self) -> None:
        settings = OperationsSettings.from_env(
            {
                "OPERATIONS_API_KEY": " operations-secret ",
                "CUSTOMER_SERVICE_DB_PATH": " data/state.db ",
            }
        )

        self.assertEqual(settings.api_key, "operations-secret")
        self.assertEqual(settings.database_path, Path("data/state.db"))

    def test_missing_key_is_allowed_but_invalid_database_path_is_rejected(self) -> None:
        settings = OperationsSettings.from_env(
            {"CUSTOMER_SERVICE_DB_PATH": "state.db"}
        )
        self.assertEqual(settings.api_key, "")

        with self.assertRaisesRegex(ValueError, "Operations configuration is invalid"):
            OperationsSettings.from_env({"CUSTOMER_SERVICE_DB_PATH": "  "})

    def test_operations_settings_repr_does_not_expose_key(self) -> None:
        settings = OperationsSettings.from_env(
            {
                "OPERATIONS_API_KEY": "operations-secret",
                "CUSTOMER_SERVICE_DB_PATH": "state.db",
            }
        )

        self.assertNotIn("operations-secret", repr(settings))


if __name__ == "__main__":
    unittest.main()
