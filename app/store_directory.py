"""Read-only, field-whitelisted access to the DingTalk store directory."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


_FIELD_LABELS = {
    "city": "城市",
    "district": "行政区",
    "opening_date": "开业时间",
    "is_physical": "是否有实体店",
    "is_management_scope": "是否管理口径",
    "usage_status": "使用情况",
    "store_type": "店型",
    "store_tier": "门店分级",
    "bed_count": "床位数",
    "is_new_store": "是否新店",
    "management_store": "对应管理门店",
}
_FIELD_KEYWORDS = {
    "city": ("城市", "哪个市"),
    "district": ("行政区", "哪个区", "区域", "在哪", "位置"),
    "opening_date": ("开业时间", "什么时候开业", "哪天开业", "开业日期"),
    "is_physical": ("实体店",),
    "is_management_scope": ("管理口径",),
    "usage_status": (
        "使用情况",
        "门店状态",
        "门店现在是什么状态",
        "门店是什么状态",
        "店现在是什么状态",
        "店是什么状态",
        "使用中",
        "待开业",
    ),
    "store_type": ("店型", "什么类型"),
    "store_tier": ("门店分级", "门店等级", "什么级别"),
    "bed_count": ("床位",),
    "is_new_store": ("是否新店", "是不是新店", "算新店", "属于新店"),
    "management_store": ("对应管理门店", "管理门店"),
}
_DEFAULT_SUMMARY_FIELDS = (
    "city",
    "district",
    "opening_date",
    "usage_status",
    "store_type",
    "store_tier",
    "bed_count",
)
_SUMMARY_TERMS = ("门店信息", "门店情况", "门店资料", "基本信息", "什么情况")
_RESTRICTED_QUERY_TERMS = (
    "手机号",
    "电话号码",
    "基本户账号",
    "银行账号",
    "银行账户",
    "法人登记证号",
    "租金",
    "管理费",
    "股东是谁",
    "股东姓名",
    "族长是谁",
    "族长姓名",
    "城市经理是谁",
    "城市经理姓名",
    "师傅股东",
    "技术股东",
)
_LEADING_QUERY_TERMS = (
    "麻烦帮我查询",
    "麻烦帮我查",
    "帮我查询",
    "帮我查一下",
    "帮我查",
    "查询一下",
    "查询",
    "查一下",
    "查看",
    "请问",
    "想问一下",
    "告诉我",
)
_QUESTION_FILLERS = (
    "什么时候",
    "是否",
    "是不是",
    "是什么",
    "有多少",
    "多少",
    "在哪个",
    "在哪里",
    "在哪",
    "怎么",
    "如何",
)
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9]+$")
_STORE_NAME_PATTERN = re.compile(
    r"(?P<name>[\u3400-\u9fffA-Za-z0-9·._-]{1,40}?)(?:门店|店)(?:的)?"
)


class StoreDirectoryError(RuntimeError):
    """The store directory could not be queried safely."""


@dataclass(frozen=True)
class StoreLookupRequest:
    store_name: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class StoreRecord:
    store_name: str
    values: Mapping[str, str]


class StoreDirectoryProvider(Protocol):
    def lookup(
        self,
        *,
        store_name: str,
        fields: Sequence[str],
    ) -> StoreRecord | None: ...


@dataclass(frozen=True)
class DwsStoreDirectoryConfig:
    base_id: str
    table_id: str
    field_ids: Mapping[str, str]
    command: str = "dws"
    timeout_seconds: int = 20

    @classmethod
    def from_file(cls, path: str | Path) -> "DwsStoreDirectoryConfig":
        config_path = Path(path)
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Unable to read store directory config: {config_path}"
            ) from exc
        if not isinstance(raw, dict):
            raise ValueError("Store directory config must be a JSON object.")

        base_id = _require_identifier(raw.get("baseId"), "baseId")
        table_id = _require_identifier(raw.get("tableId"), "tableId")
        raw_field_ids = raw.get("fieldIds")
        if not isinstance(raw_field_ids, dict):
            raise ValueError("fieldIds must be a JSON object.")
        required_fields = {"store_name", *_FIELD_LABELS}
        if set(raw_field_ids) != required_fields:
            raise ValueError(
                "fieldIds must contain exactly the supported store directory fields."
            )
        field_ids = {
            key: _require_identifier(value, f"fieldIds.{key}")
            for key, value in raw_field_ids.items()
        }
        command = raw.get("command", "dws")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string.")
        timeout_seconds = raw.get("timeoutSeconds", 20)
        if (
            not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or not 1 <= timeout_seconds <= 60
        ):
            raise ValueError("timeoutSeconds must be an integer from 1 to 60.")
        return cls(
            base_id=base_id,
            table_id=table_id,
            field_ids=field_ids,
            command=command.strip(),
            timeout_seconds=timeout_seconds,
        )


DwsCommandRunner = Callable[[Sequence[str], int], Mapping[str, object]]


class DwsStoreDirectory:
    """Query only approved fields through the local DingTalk workspace CLI."""

    def __init__(
        self,
        config: DwsStoreDirectoryConfig,
        *,
        runner: DwsCommandRunner | None = None,
    ) -> None:
        self._config = config
        self._runner = runner or _run_dws_json

    def lookup(
        self,
        *,
        store_name: str,
        fields: Sequence[str],
    ) -> StoreRecord | None:
        normalized_name = _normalize_store_name(store_name)
        requested_fields = tuple(dict.fromkeys(fields))
        if not normalized_name:
            raise ValueError("store_name is required.")
        if not requested_fields or any(
            field not in _FIELD_LABELS for field in requested_fields
        ):
            raise ValueError("Only approved store fields may be queried.")

        name_field_id = self._config.field_ids["store_name"]
        search_payload = self._query(
            "--field-ids",
            name_field_id,
            "--query",
            normalized_name,
            "--limit",
            "10",
        )
        matching_record = self._find_exact_record(
            search_payload,
            field_id=name_field_id,
            store_name=normalized_name,
        )
        if matching_record is None:
            return None

        record_id = matching_record["recordId"]
        if not isinstance(record_id, str) or not _IDENTIFIER_PATTERN.fullmatch(
            record_id
        ):
            raise StoreDirectoryError("DingTalk returned an invalid record ID.")

        def read_field(field: str) -> tuple[str, str]:
            field_id = self._config.field_ids[field]
            payload = self._query(
                "--record-ids",
                record_id,
                "--field-ids",
                field_id,
                "--limit",
                "1",
            )
            records = _records_from_payload(payload)
            raw_value = (
                records[0].get("cells", {}).get(field_id) if records else None
            )
            return field, _format_cell_value(raw_value)

        values: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(requested_fields))) as executor:
            for field, value in executor.map(read_field, requested_fields):
                values[field] = value

        cells = matching_record.get("cells", {})
        matched_name = (
            cells.get(name_field_id)
            if isinstance(cells, dict)
            else normalized_name
        )
        return StoreRecord(
            store_name=(
                matched_name.strip()
                if isinstance(matched_name, str) and matched_name.strip()
                else normalized_name
            ),
            values=values,
        )

    def _query(self, *arguments: str) -> Mapping[str, object]:
        payload = self._runner(
            (
                self._config.command,
                "aitable",
                "record",
                "list",
                "--base-id",
                self._config.base_id,
                "--table-id",
                self._config.table_id,
                *arguments,
                "--format",
                "json",
            ),
            self._config.timeout_seconds,
        )
        if payload.get("success") is not True or payload.get("status") != "success":
            raise StoreDirectoryError("DingTalk store directory query failed.")
        return payload

    @staticmethod
    def _find_exact_record(
        payload: Mapping[str, object],
        *,
        field_id: str,
        store_name: str,
    ) -> Mapping[str, object] | None:
        normalized_target = _normalize_store_name(store_name)
        matches = []
        for record in _records_from_payload(payload):
            cells = record.get("cells")
            value = cells.get(field_id) if isinstance(cells, dict) else None
            if (
                isinstance(value, str)
                and _normalize_store_name(value) == normalized_target
            ):
                matches.append(record)
        return matches[0] if len(matches) == 1 else None


class HTTPResponse(Protocol):
    def __enter__(self) -> "HTTPResponse": ...

    def __exit__(self, *args: object) -> None: ...

    def read(self) -> bytes: ...


OpenUrl = Callable[..., HTTPResponse]
Clock = Callable[[], float]


@dataclass(frozen=True)
class DingTalkOpenAPIStoreDirectoryConfig:
    """Server-side credentials and read limits for the Notable OpenAPI."""

    app_key: str
    app_secret: str = field(repr=False)
    operator_id: str
    base_id: str
    table_id: str
    field_ids: Mapping[str, str]
    timeout_seconds: int = 20
    page_size: int = 100
    max_pages: int = 20

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        app_key: str,
        app_secret: str,
        operator_id: str,
    ) -> "DingTalkOpenAPIStoreDirectoryConfig":
        dws_config = DwsStoreDirectoryConfig.from_file(path)
        normalized_app_key = _require_nonempty_text(app_key, "DingTalk app key")
        normalized_app_secret = _require_nonempty_text(
            app_secret,
            "DingTalk app secret",
        )
        normalized_operator_id = _require_nonempty_text(
            operator_id,
            "DingTalk store operator unionId",
        )
        return cls(
            app_key=normalized_app_key,
            app_secret=normalized_app_secret,
            operator_id=normalized_operator_id,
            base_id=dws_config.base_id,
            table_id=dws_config.table_id,
            field_ids=dws_config.field_ids,
            timeout_seconds=dws_config.timeout_seconds,
        )


class _DingTalkHTTPError(StoreDirectoryError):
    def __init__(
        self,
        status_code: int,
        *,
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        details = [
            detail
            for detail in (
                f"code={error_code}" if error_code else "",
                f"message={error_message}" if error_message else "",
            )
            if detail
        ]
        suffix = f" ({'; '.join(details)})" if details else ""
        super().__init__(
            f"DingTalk OpenAPI returned HTTP {status_code}{suffix}."
        )
        self.status_code = status_code
        self.error_code = error_code


class DingTalkOpenAPIStoreDirectory:
    """Read approved store fields with a DingTalk internal-app identity."""

    _API_BASE_URL = "https://api.dingtalk.com"
    _TOKEN_REFRESH_SKEW_SECONDS = 60

    def __init__(
        self,
        config: DingTalkOpenAPIStoreDirectoryConfig,
        *,
        opener: OpenUrl = urlopen,
        clock: Clock = monotonic,
    ) -> None:
        if not 1 <= config.page_size <= 100:
            raise ValueError("DingTalk page size must be from 1 to 100.")
        if not 1 <= config.max_pages <= 100:
            raise ValueError("DingTalk max pages must be from 1 to 100.")
        self._config = config
        self._opener = opener
        self._clock = clock
        self._token_lock = Lock()
        self._access_token = ""
        self._access_token_expires_at = 0.0

    def lookup(
        self,
        *,
        store_name: str,
        fields: Sequence[str],
    ) -> StoreRecord | None:
        normalized_name = _normalize_store_name(store_name)
        requested_fields = tuple(dict.fromkeys(fields))
        if not normalized_name:
            raise ValueError("store_name is required.")
        if not requested_fields or any(
            field not in _FIELD_LABELS for field in requested_fields
        ):
            raise ValueError("Only approved store fields may be queried.")

        name_field_id = self._config.field_ids["store_name"]
        requested_field_ids = [
            name_field_id,
            *(self._config.field_ids[field] for field in requested_fields),
        ]
        matches: list[Mapping[str, object]] = []
        next_token = ""
        seen_tokens: set[str] = set()

        for _ in range(self._config.max_pages):
            payload = self._list_records_page(
                field_ids=requested_field_ids,
                next_token=next_token,
            )
            records = payload.get("records")
            if not isinstance(records, list):
                raise StoreDirectoryError(
                    "DingTalk OpenAPI returned invalid store records."
                )
            for record in records:
                if not isinstance(record, dict):
                    raise StoreDirectoryError(
                        "DingTalk OpenAPI returned invalid store records."
                    )
                raw_fields = record.get("fields")
                value = (
                    raw_fields.get(name_field_id)
                    if isinstance(raw_fields, dict)
                    else None
                )
                if (
                    isinstance(value, str)
                    and _normalize_store_name(value) == normalized_name
                ):
                    matches.append(record)

            has_more = payload.get("hasMore")
            if has_more is not True:
                break
            returned_token = payload.get("nextToken")
            if (
                not isinstance(returned_token, str)
                or not returned_token
                or returned_token in seen_tokens
            ):
                raise StoreDirectoryError(
                    "DingTalk OpenAPI returned invalid pagination state."
                )
            seen_tokens.add(returned_token)
            next_token = returned_token
        else:
            raise StoreDirectoryError(
                "DingTalk store directory exceeded the configured read limit."
            )

        if len(matches) != 1:
            return None
        matched_fields = matches[0].get("fields")
        if not isinstance(matched_fields, dict):
            raise StoreDirectoryError(
                "DingTalk OpenAPI returned invalid store fields."
            )
        matched_name = matched_fields.get(name_field_id)
        return StoreRecord(
            store_name=(
                matched_name.strip()
                if isinstance(matched_name, str) and matched_name.strip()
                else normalized_name
            ),
            values={
                field_name: _format_cell_value(
                    matched_fields.get(self._config.field_ids[field_name])
                )
                for field_name in requested_fields
            },
        )

    def _list_records_page(
        self,
        *,
        field_ids: Sequence[str],
        next_token: str,
    ) -> Mapping[str, object]:
        path = (
            f"/v1.0/notable/bases/{quote(self._config.base_id, safe='')}"
            f"/sheets/{quote(self._config.table_id, safe='')}/records/list?"
            f"{urlencode({'operatorId': self._config.operator_id})}"
        )
        payload: dict[str, object] = {
            "calcFields": False,
            "fieldIdOrNames": list(field_ids),
            "maxResults": self._config.page_size,
        }
        if next_token:
            payload["nextToken"] = next_token

        token = self._get_access_token()
        try:
            return self._post_json(path, payload, access_token=token)
        except _DingTalkHTTPError as exc:
            if exc.status_code != 401:
                raise
            self._invalidate_access_token(token)
            return self._post_json(
                path,
                payload,
                access_token=self._get_access_token(),
            )

    def _get_access_token(self) -> str:
        now = self._clock()
        if self._access_token and now < self._access_token_expires_at:
            return self._access_token
        with self._token_lock:
            now = self._clock()
            if self._access_token and now < self._access_token_expires_at:
                return self._access_token
            payload = self._post_json(
                "/v1.0/oauth2/accessToken",
                {
                    "appKey": self._config.app_key,
                    "appSecret": self._config.app_secret,
                },
            )
            access_token = payload.get("accessToken")
            expire_in = payload.get("expireIn")
            if (
                not isinstance(access_token, str)
                or not access_token.strip()
                or not isinstance(expire_in, int)
                or isinstance(expire_in, bool)
                or expire_in <= 0
            ):
                raise StoreDirectoryError(
                    "DingTalk OpenAPI returned an invalid access token."
                )
            self._access_token = access_token.strip()
            usable_seconds = max(
                1,
                expire_in - self._TOKEN_REFRESH_SKEW_SECONDS,
            )
            self._access_token_expires_at = now + usable_seconds
            return self._access_token

    def _invalidate_access_token(self, rejected_token: str) -> None:
        with self._token_lock:
            if self._access_token == rejected_token:
                self._access_token = ""
                self._access_token_expires_at = 0.0

    def _post_json(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        access_token: str = "",
    ) -> Mapping[str, object]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if access_token:
            headers["x-acs-dingtalk-access-token"] = access_token
        request = Request(
            f"{self._API_BASE_URL}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self._opener(
                request,
                timeout=self._config.timeout_seconds,
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            error_code, error_message = _parse_dingtalk_http_error(
                exc,
                sensitive_values=(
                    self._config.app_secret,
                    access_token,
                    self._config.operator_id,
                ),
            )
            raise _DingTalkHTTPError(
                exc.code,
                error_code=error_code,
                error_message=error_message,
            ) from exc
        except URLError as exc:
            raise StoreDirectoryError(
                "DingTalk OpenAPI is unreachable."
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreDirectoryError(
                "DingTalk OpenAPI returned invalid JSON."
            ) from exc
        if not isinstance(result, dict):
            raise StoreDirectoryError(
                "DingTalk OpenAPI returned an invalid response."
            )
        return result


def load_store_directory(
    environ: Mapping[str, str],
) -> StoreDirectoryProvider | None:
    path_text = environ.get(
        "DINGTALK_STORE_CONFIG_PATH",
        "config/store-directory.local.json",
    ).strip()
    if not path_text:
        return None
    path = Path(path_text)
    if not path.is_file():
        return None
    provider = environ.get("DINGTALK_STORE_PROVIDER", "dws").strip().lower()
    if provider == "dws":
        return DwsStoreDirectory(DwsStoreDirectoryConfig.from_file(path))
    if provider == "openapi":
        return DingTalkOpenAPIStoreDirectory(
            DingTalkOpenAPIStoreDirectoryConfig.from_file(
                path,
                app_key=environ.get("DINGTALK_CLIENT_ID", ""),
                app_secret=environ.get("DINGTALK_CLIENT_SECRET", ""),
                operator_id=environ.get("DINGTALK_STORE_OPERATOR_ID", ""),
            )
        )
    raise ValueError("DINGTALK_STORE_PROVIDER must be 'dws' or 'openapi'.")


def parse_store_lookup(question: str) -> StoreLookupRequest | None:
    normalized_question = question.strip()
    if not normalized_question or any(
        term in normalized_question for term in _RESTRICTED_QUERY_TERMS
    ):
        return None

    fields = tuple(
        field
        for field, keywords in _FIELD_KEYWORDS.items()
        if any(keyword in normalized_question for keyword in keywords)
    )
    if not fields and any(
        term in normalized_question for term in _SUMMARY_TERMS
    ):
        fields = _DEFAULT_SUMMARY_FIELDS
    if not fields:
        return None

    store_name = _extract_store_name(normalized_question)
    if not store_name:
        return None
    return StoreLookupRequest(store_name=store_name, fields=fields)


def format_store_answer(record: StoreRecord, fields: Sequence[str]) -> str:
    lines = [f"{record.store_name}门店信息："]
    for field in fields:
        lines.append(f"- {_FIELD_LABELS[field]}：{record.values.get(field, '未维护')}")
    lines.append("")
    lines.append("数据来源：门店信息宽表（实时只读）")
    return "\n".join(lines)


def _extract_store_name(question: str) -> str:
    compact = re.sub(r"\s+", "", question)
    for term in _LEADING_QUERY_TERMS:
        if compact.startswith(term):
            compact = compact[len(term) :]
            break

    match = _STORE_NAME_PATTERN.search(compact)
    if match:
        candidate = match.group("name")
    else:
        keyword_positions = [
            compact.find(keyword)
            for keywords in _FIELD_KEYWORDS.values()
            for keyword in keywords
            if compact.find(keyword) >= 0
        ]
        keyword_positions.extend(
            compact.find(term)
            for term in _SUMMARY_TERMS
            if compact.find(term) >= 0
        )
        if not keyword_positions:
            return ""
        candidate = compact[: min(keyword_positions)].rstrip("的")

    for filler in _QUESTION_FILLERS:
        candidate = candidate.replace(filler, "")
    candidate = candidate.strip("，。！？?：:；;、的")
    candidate = re.sub(r"(?:门店|店)$", "", candidate)
    if candidate in {"", "某", "这个", "该", "一家", "门店"}:
        return ""
    if not 1 <= len(candidate) <= 40:
        return ""
    return candidate


def _normalize_store_name(value: str) -> str:
    normalized = re.sub(r"\s+", "", value).strip("，。！？?：:；;、")
    return re.sub(r"(?:门店|店)$", "", normalized)


def _format_cell_value(value: object) -> str:
    if value is None or value == "":
        return "未维护"
    if isinstance(value, dict):
        name = value.get("name")
        return str(name).strip() if name not in (None, "") else "未维护"
    if isinstance(value, list):
        names = [
            str(item.get("name")).strip()
            for item in value
            if isinstance(item, dict) and item.get("name") not in (None, "")
        ]
        return "、".join(names) if names else "未维护"
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value.strip() or "未维护"
        return parsed.date().isoformat()
    return str(value)


def _records_from_payload(
    payload: Mapping[str, object],
) -> list[Mapping[str, object]]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    records = data.get("records")
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def _run_dws_json(
    arguments: Sequence[str],
    timeout_seconds: int,
) -> Mapping[str, object]:
    if not arguments:
        raise StoreDirectoryError("DWS command is not configured.")
    command = shutil.which(arguments[0]) or arguments[0]
    try:
        completed = subprocess.run(
            (command, *arguments[1:]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StoreDirectoryError("DWS command could not be executed.") from exc
    if completed.returncode != 0:
        raise StoreDirectoryError("DWS command returned an error.")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise StoreDirectoryError("DWS returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise StoreDirectoryError("DWS returned an invalid response.")
    return payload


def _require_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must contain only letters and digits.")
    return value


def _require_nonempty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required.")
    return value.strip()


def _parse_dingtalk_http_error(
    error: HTTPError,
    *,
    sensitive_values: Sequence[str],
) -> tuple[str, str]:
    try:
        payload = json.loads(error.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "", ""
    if not isinstance(payload, dict):
        return "", ""

    raw_code = payload.get("code", payload.get("errcode", ""))
    raw_message = payload.get("message", payload.get("errmsg", ""))
    code = _sanitize_error_text(raw_code, sensitive_values, limit=80)
    message = _sanitize_error_text(raw_message, sensitive_values, limit=240)
    return code, message


def _sanitize_error_text(
    value: object,
    sensitive_values: Sequence[str],
    *,
    limit: int,
) -> str:
    if not isinstance(value, (str, int)):
        return ""
    text = " ".join(str(value).split())
    for sensitive_value in sensitive_values:
        if sensitive_value:
            text = text.replace(sensitive_value, "[REDACTED]")
    return text[:limit]
