import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from app.enterprise_config import load_enterprise_config, main


class EnterpriseConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    @staticmethod
    def valid_payload() -> dict[str, object]:
        return {
            "schema_version": 1,
            "company": {
                "name": "测试商城",
                "industry": "ecommerce",
                "website_url": "https://shop.example.com",
            },
            "assistant": {
                "name": "测试商城 AI 客服",
                "welcome_title": "欢迎咨询测试商城",
                "welcome_message": "请告诉我你想了解的问题。",
                "handoff_message": "正在转接人工客服。",
            },
            "dify": {
                "dataset_name": "测试商城客服知识库",
                "app_name": "测试商城 AI 客服",
                "indexing_technique": "economy",
            },
            "chatwoot": {
                "inbox_name": "测试商城网站客服",
                "widget_color": "#1f6feb",
            },
            "locale": "zh_CN",
            "business_timezone": "Asia/Shanghai",
        }

    def write_config(
        self,
        payload: object,
        *,
        name: str = "enterprise.json",
        encoding: str = "utf-8",
    ) -> Path:
        path = self.root / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding=encoding,
        )
        return path

    def test_valid_config_loads_as_immutable_values(self) -> None:
        config = load_enterprise_config(self.write_config(self.valid_payload()))

        self.assertEqual(config.company.name, "测试商城")
        self.assertEqual(config.assistant.handoff_message, "正在转接人工客服。")
        self.assertEqual(config.dify.indexing_technique, "economy")
        self.assertEqual(config.to_mapping()["chatwoot"]["widget_color"], "#1f6feb")
        with self.assertRaisesRegex(Exception, "cannot assign"):
            config.company.name = "另一个商城"  # type: ignore[misc]

    def test_unknown_or_secret_like_fields_are_rejected(self) -> None:
        payload = self.valid_payload()
        payload["api_key"] = "must-not-be-accepted"

        with self.assertRaisesRegex(ValueError, "api_key"):
            load_enterprise_config(self.write_config(payload))

    def test_invalid_values_report_the_field_path(self) -> None:
        cases = (
            ("company.website_url", "ftp://example.com"),
            ("chatwoot.widget_color", "blue"),
            ("company.industry", "finance"),
            ("locale", "en_US"),
            ("assistant.handoff_message", "x" * 501),
            ("business_timezone", "UTC"),
        )
        for field_path, invalid in cases:
            with self.subTest(field_path=field_path):
                payload = self.valid_payload()
                target = payload
                parts = field_path.split(".")
                for part in parts[:-1]:
                    target = target[part]  # type: ignore[index,assignment]
                target[parts[-1]] = invalid  # type: ignore[index]
                with self.assertRaisesRegex(ValueError, field_path):
                    load_enterprise_config(self.write_config(payload))

    def test_missing_invalid_json_and_wrong_types_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "could not be read"):
            load_enterprise_config(self.root / "missing.json")

        invalid_json = self.root / "invalid.json"
        invalid_json.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "valid JSON"):
            load_enterprise_config(invalid_json)

        payload = self.valid_payload()
        del payload["assistant"]
        with self.assertRaisesRegex(ValueError, "assistant"):
            load_enterprise_config(self.write_config(payload))

        payload = self.valid_payload()
        payload["company"] = []
        with self.assertRaisesRegex(ValueError, "company"):
            load_enterprise_config(self.write_config(payload))

    def test_schema_text_limits_and_supported_indexing_are_enforced(self) -> None:
        payload = self.valid_payload()
        payload["schema_version"] = 2
        with self.assertRaisesRegex(ValueError, "schema_version"):
            load_enterprise_config(self.write_config(payload))

        payload = self.valid_payload()
        payload["company"]["name"] = "x" * 101  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "company.name"):
            load_enterprise_config(self.write_config(payload))

        payload = self.valid_payload()
        payload["dify"]["indexing_technique"] = "high_quality"  # type: ignore[index]
        config = load_enterprise_config(self.write_config(payload, encoding="utf-8-sig"))
        self.assertEqual(config.dify.indexing_technique, "high_quality")

    def test_cli_validates_and_prints_sanitized_configuration(self) -> None:
        path = self.write_config(self.valid_payload())
        stdout = StringIO()
        with redirect_stdout(stdout):
            result = main(["--path", str(path), "--print-json"])

        self.assertEqual(result, 0)
        printed = json.loads(stdout.getvalue())
        self.assertEqual(printed["company"]["name"], "测试商城")

        stderr = StringIO()
        with redirect_stderr(stderr):
            result = main(["--path", str(self.root / "missing.json")])
        self.assertEqual(result, 2)
        self.assertIn("ERROR:", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
