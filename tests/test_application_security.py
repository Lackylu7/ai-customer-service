import unittest

from fastapi.testclient import TestClient

from app.main import create_fastapi_application
from app.settings import ApplicationSettings


class ApplicationSecurityTests(unittest.TestCase):
    def test_production_requires_explicit_non_wildcard_hosts(self) -> None:
        for allowed_hosts in ("", "*"):
            with self.subTest(allowed_hosts=allowed_hosts), self.assertRaises(ValueError):
                ApplicationSettings.from_env(
                    {
                        "APP_ENV": "production",
                        "APP_ALLOWED_HOSTS": allowed_hosts,
                    }
                )

    def test_production_disables_api_docs_and_rejects_unknown_hosts(self) -> None:
        settings = ApplicationSettings.from_env(
            {
                "APP_ENV": "production",
                "APP_ALLOWED_HOSTS": "ops.example.com,support.example.com",
            }
        )
        application = create_fastapi_application(settings)

        self.assertIsNone(application.docs_url)
        self.assertIsNone(application.redoc_url)
        self.assertIsNone(application.openapi_url)

        with TestClient(application) as client:
            rejected = client.get("/health", headers={"Host": "attacker.example"})
            accepted = client.get("/health", headers={"Host": "ops.example.com"})

        self.assertEqual(rejected.status_code, 400)
        self.assertNotEqual(accepted.status_code, 400)

    def test_security_headers_are_present_without_leaking_server_details(self) -> None:
        application = create_fastapi_application(
            ApplicationSettings.from_env({"APP_ENV": "development"})
        )
        with TestClient(application) as client:
            response = client.get("/not-found")

        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertNotIn("server", response.headers)
        self.assertNotIn("strict-transport-security", response.headers)

    def test_production_adds_hsts(self) -> None:
        application = create_fastapi_application(
            ApplicationSettings.from_env(
                {
                    "APP_ENV": "production",
                    "APP_ALLOWED_HOSTS": "ops.example.com",
                }
            )
        )
        with TestClient(application) as client:
            response = client.get("/not-found", headers={"Host": "ops.example.com"})

        self.assertEqual(
            response.headers["strict-transport-security"],
            "max-age=31536000; includeSubDomains",
        )


if __name__ == "__main__":
    unittest.main()
