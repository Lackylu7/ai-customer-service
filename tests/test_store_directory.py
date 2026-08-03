import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from app.store_directory import (
    DingTalkOpenAPIStoreDirectory,
    DingTalkOpenAPIStoreDirectoryConfig,
    DwsStoreDirectory,
    DwsStoreDirectoryConfig,
    load_store_directory,
    parse_store_lookup,
)


FIELD_IDS = {
    "store_name": "StoreName1",
    "city": "City1",
    "district": "District1",
    "opening_date": "Opening1",
    "is_physical": "Physical1",
    "is_management_scope": "Management1",
    "usage_status": "Usage1",
    "store_type": "Type1",
    "store_tier": "Tier1",
    "bed_count": "Beds1",
    "is_new_store": "New1",
    "management_store": "ManagementStore1",
}


class FakeHTTPResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")


class StoreQueryParserTests(unittest.TestCase):
    def test_extracts_store_and_requested_field(self) -> None:
        request = parse_store_lookup("会展中心店什么时候开业？")

        self.assertIsNotNone(request)
        self.assertEqual(request.store_name, "会展中心")
        self.assertEqual(request.fields, ("opening_date",))

    def test_recognizes_natural_store_status_question(self) -> None:
        request = parse_store_lookup("会展中心门店现在是什么状态？")

        self.assertIsNotNone(request)
        self.assertEqual(request.store_name, "会展中心")
        self.assertEqual(request.fields, ("usage_status",))

    def test_summary_query_uses_only_the_safe_summary_fields(self) -> None:
        request = parse_store_lookup("帮我查红山门店信息")

        self.assertIsNotNone(request)
        self.assertEqual(request.store_name, "红山")
        self.assertNotIn("phone", request.fields)
        self.assertNotIn("shareholder", request.fields)

    def test_restricted_person_query_is_not_parsed_as_safe_lookup(self) -> None:
        self.assertIsNone(parse_store_lookup("会展中心门店的城市经理是谁？"))

    def test_shareholder_new_store_question_is_not_a_directory_lookup(self) -> None:
        self.assertIsNone(parse_store_lookup("新店入股是不是固定25万元？"))

    def test_explicit_new_store_attribute_question_is_a_directory_lookup(self) -> None:
        request = parse_store_lookup("会展中心店是不是新店？")

        self.assertIsNotNone(request)
        self.assertEqual(request.store_name, "会展中心")
        self.assertEqual(request.fields, ("is_new_store",))


class DwsStoreDirectoryTests(unittest.TestCase):
    def test_queries_only_requested_whitelisted_fields(self) -> None:
        calls: list[tuple[str, ...]] = []

        def runner(arguments, timeout_seconds):
            self.assertEqual(timeout_seconds, 5)
            arguments = tuple(arguments)
            calls.append(arguments)
            field_id = arguments[arguments.index("--field-ids") + 1]
            if "--query" in arguments:
                return {
                    "success": True,
                    "status": "success",
                    "data": {
                        "records": [
                            {
                                "recordId": "Record1",
                                "cells": {"StoreName1": "会展中心"},
                            }
                        ]
                    },
                }
            values = {
                "Opening1": "2023-06-05T00:00:00+08:00",
                "Usage1": {"name": "使用中"},
            }
            return {
                "success": True,
                "status": "success",
                "data": {
                    "records": [
                        {
                            "recordId": "Record1",
                            "cells": {field_id: values[field_id]},
                        }
                    ]
                },
            }

        directory = DwsStoreDirectory(
            DwsStoreDirectoryConfig(
                base_id="Base1",
                table_id="Table1",
                field_ids=FIELD_IDS,
                timeout_seconds=5,
            ),
            runner=runner,
        )

        record = directory.lookup(
            store_name="会展中心店",
            fields=("opening_date", "usage_status"),
        )

        self.assertIsNotNone(record)
        self.assertEqual(record.store_name, "会展中心")
        self.assertEqual(record.values["opening_date"], "2023-06-05")
        self.assertEqual(record.values["usage_status"], "使用中")
        queried_field_ids = {
            call[call.index("--field-ids") + 1] for call in calls
        }
        self.assertEqual(queried_field_ids, {"StoreName1", "Opening1", "Usage1"})

    def test_config_rejects_missing_field_whitelist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.json"
            path.write_text(
                '{"baseId":"Base1","tableId":"Table1","fieldIds":{}}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "exactly"):
                DwsStoreDirectoryConfig.from_file(path)


class DingTalkOpenAPIStoreDirectoryTests(unittest.TestCase):
    def test_uses_app_token_and_reads_only_requested_fields_across_pages(
        self,
    ) -> None:
        calls = []

        def opener(request, timeout):
            self.assertEqual(timeout, 5)
            calls.append(request)
            payload = json.loads(request.data.decode("utf-8"))
            if request.full_url.endswith("/v1.0/oauth2/accessToken"):
                self.assertEqual(
                    payload,
                    {"appKey": "app-key", "appSecret": "private-secret"},
                )
                return FakeHTTPResponse(
                    {"accessToken": "access-token", "expireIn": 7200}
                )

            query = parse_qs(urlparse(request.full_url).query)
            self.assertEqual(query, {"operatorId": ["operator-union-id"]})
            self.assertEqual(
                payload["fieldIdOrNames"],
                ["StoreName1", "Opening1", "Usage1"],
            )
            self.assertNotIn("Phone1", payload["fieldIdOrNames"])
            self.assertEqual(payload["calcFields"], False)
            self.assertEqual(payload["maxResults"], 2)
            self.assertEqual(
                request.get_header("X-acs-dingtalk-access-token"),
                "access-token",
            )
            if "nextToken" not in payload:
                return FakeHTTPResponse(
                    {
                        "hasMore": True,
                        "nextToken": "page-2",
                        "records": [
                            {
                                "id": "Record1",
                                "fields": {
                                    "StoreName1": "红山",
                                    "Opening1": "2022-01-01",
                                    "Usage1": {"name": "使用中"},
                                },
                            }
                        ],
                    }
                )
            self.assertEqual(payload["nextToken"], "page-2")
            return FakeHTTPResponse(
                {
                    "hasMore": False,
                    "records": [
                        {
                            "id": "Record2",
                            "fields": {
                                "StoreName1": "会展中心",
                                "Opening1": "2023-06-05T00:00:00+08:00",
                                "Usage1": {"name": "使用中"},
                            },
                        }
                    ],
                }
            )

        config = DingTalkOpenAPIStoreDirectoryConfig(
            app_key="app-key",
            app_secret="private-secret",
            operator_id="operator-union-id",
            base_id="Base1",
            table_id="Table1",
            field_ids=FIELD_IDS,
            timeout_seconds=5,
            page_size=2,
        )
        directory = DingTalkOpenAPIStoreDirectory(
            config,
            opener=opener,
        )

        record = directory.lookup(
            store_name="会展中心店",
            fields=("opening_date", "usage_status"),
        )

        self.assertIsNotNone(record)
        self.assertEqual(record.store_name, "会展中心")
        self.assertEqual(record.values["opening_date"], "2023-06-05")
        self.assertEqual(record.values["usage_status"], "使用中")
        self.assertEqual(
            len(
                [
                    call
                    for call in calls
                    if call.full_url.endswith("/v1.0/oauth2/accessToken")
                ]
            ),
            1,
        )
        self.assertNotIn("private-secret", repr(config))

    def test_http_error_includes_sanitized_dingtalk_details(self) -> None:
        def opener(request, timeout):
            del timeout
            if request.full_url.endswith("/v1.0/oauth2/accessToken"):
                return FakeHTTPResponse(
                    {"accessToken": "access-token", "expireIn": 7200}
                )
            body = json.dumps(
                {
                    "code": "InvalidParameter",
                    "message": (
                        "operator operator-union-id rejected; "
                        "token access-token; secret private-secret"
                    ),
                }
            ).encode("utf-8")
            raise HTTPError(
                request.full_url,
                400,
                "Bad Request",
                {},
                BytesIO(body),
            )

        directory = DingTalkOpenAPIStoreDirectory(
            DingTalkOpenAPIStoreDirectoryConfig(
                app_key="app-key",
                app_secret="private-secret",
                operator_id="operator-union-id",
                base_id="Base1",
                table_id="Table1",
                field_ids=FIELD_IDS,
            ),
            opener=opener,
        )

        with self.assertRaisesRegex(
            Exception,
            r"HTTP 400.*code=InvalidParameter.*\[REDACTED\]",
        ) as raised:
            directory.lookup(
                store_name="会展中心",
                fields=("opening_date",),
            )

        message = str(raised.exception)
        self.assertNotIn("operator-union-id", message)
        self.assertNotIn("access-token", message)
        self.assertNotIn("private-secret", message)

    def test_loader_selects_server_openapi_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.json"
            path.write_text(
                json.dumps(
                    {
                        "baseId": "Base1",
                        "tableId": "Table1",
                        "fieldIds": FIELD_IDS,
                        "timeoutSeconds": 5,
                    }
                ),
                encoding="utf-8",
            )

            provider = load_store_directory(
                {
                    "DINGTALK_STORE_CONFIG_PATH": str(path),
                    "DINGTALK_STORE_PROVIDER": "openapi",
                    "DINGTALK_CLIENT_ID": "app-key",
                    "DINGTALK_CLIENT_SECRET": "private-secret",
                    "DINGTALK_STORE_OPERATOR_ID": "operator-union-id",
                }
            )

        self.assertIsInstance(provider, DingTalkOpenAPIStoreDirectory)

    def test_loader_fails_fast_when_openapi_identity_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.json"
            path.write_text(
                json.dumps(
                    {
                        "baseId": "Base1",
                        "tableId": "Table1",
                        "fieldIds": FIELD_IDS,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "app key"):
                load_store_directory(
                    {
                        "DINGTALK_STORE_CONFIG_PATH": str(path),
                        "DINGTALK_STORE_PROVIDER": "openapi",
                    }
                )


if __name__ == "__main__":
    unittest.main()
