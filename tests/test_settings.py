import json
import tempfile
import unittest
from pathlib import Path

from app.settings import (
    HandoffOperationsSettings,
    OperationsSettings,
    Settings,
    WorkerSettings,
)


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

    def test_loads_enterprise_configuration_from_explicit_path(self) -> None:
        payload = json.loads(
            Path("config/enterprise.example.json").read_text(encoding="utf-8")
        )
        payload["company"]["name"] = "测试商城"
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "enterprise.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            environment = valid_environment()
            environment["ENTERPRISE_CONFIG_PATH"] = str(path)

            settings = Settings.from_env(environment)

        self.assertEqual(settings.enterprise_config.company.name, "测试商城")


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


class WorkerSettingsTests(unittest.TestCase):
    def test_defaults_match_single_server_design(self) -> None:
        settings = WorkerSettings.from_env({})

        self.assertEqual(settings.concurrency, 2)
        self.assertEqual(settings.lease_seconds, 300)
        self.assertEqual(settings.reconcile_interval_seconds, 60)
        self.assertEqual(settings.generation_max_attempts, 3)
        self.assertEqual(settings.delivery_max_attempts, 5)
        self.assertEqual(settings.human_sla_seconds, 300)
        self.assertEqual(settings.human_fallback_cooldown_seconds, 300)

    def test_rejects_out_of_range_values(self) -> None:
        invalid = {
            "CUSTOMER_SERVICE_WORKER_CONCURRENCY": "0",
            "CUSTOMER_SERVICE_JOB_LEASE_SECONDS": "30",
        }

        with self.assertRaisesRegex(ValueError, "Worker configuration is invalid"):
            WorkerSettings.from_env(invalid)

    def test_loads_short_human_sla_for_automated_acceptance(self) -> None:
        settings = WorkerSettings.from_env(
            {
                "CUSTOMER_SERVICE_HUMAN_SLA_SECONDS": "3",
                "CUSTOMER_SERVICE_HUMAN_FALLBACK_COOLDOWN_SECONDS": "7",
            }
        )

        self.assertEqual(settings.human_sla_seconds, 3)
        self.assertEqual(settings.human_fallback_cooldown_seconds, 7)


class HandoffOperationsSettingsTests(unittest.TestCase):
    def test_loads_only_chatwoot_control_and_sla_values(self) -> None:
        settings = HandoffOperationsSettings.from_env(
            {
                "CHATWOOT_BASE_URL": " http://host.docker.internal:3000/ ",
                "CHATWOOT_PUBLIC_BASE_URL": " http://127.0.0.1:3000/ ",
                "CHATWOOT_ACCOUNT_ID": "1",
                "CHATWOOT_API_TOKEN": "control-secret",
                "CUSTOMER_SERVICE_DB_PATH": "data/state.db",
                "CUSTOMER_SERVICE_HUMAN_SLA_SECONDS": "300",
            }
        )

        self.assertEqual(
            settings.chatwoot_base_url,
            "http://host.docker.internal:3000",
        )
        self.assertEqual(
            settings.chatwoot_public_base_url,
            "http://127.0.0.1:3000",
        )
        self.assertEqual(settings.chatwoot_account_id, 1)
        self.assertEqual(settings.database_path, Path("data/state.db"))
        self.assertEqual(settings.human_sla_seconds, 300)
        self.assertNotIn("control-secret", repr(settings))

    def test_rejects_missing_control_token_or_invalid_sla(self) -> None:
        environment = {
            "CHATWOOT_BASE_URL": "http://127.0.0.1:3000",
            "CHATWOOT_ACCOUNT_ID": "1",
            "CHATWOOT_API_TOKEN": "",
            "CUSTOMER_SERVICE_DB_PATH": "data/state.db",
            "CUSTOMER_SERVICE_HUMAN_SLA_SECONDS": "0",
        }

        with self.assertRaisesRegex(
            ValueError,
            "Human handoff operations configuration is invalid",
        ):
            HandoffOperationsSettings.from_env(environment)


if __name__ == "__main__":
    unittest.main()
