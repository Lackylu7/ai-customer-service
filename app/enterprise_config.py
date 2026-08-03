from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Mapping, Sequence, cast
from urllib.parse import urlsplit


@dataclass(frozen=True)
class CompanyConfig:
    name: str
    industry: Literal["ecommerce"]
    website_url: str


@dataclass(frozen=True)
class AssistantConfig:
    name: str
    welcome_title: str
    welcome_message: str
    handoff_message: str


@dataclass(frozen=True)
class DifyEnterpriseConfig:
    dataset_name: str
    app_name: str
    indexing_technique: Literal["economy", "high_quality"]


@dataclass(frozen=True)
class ChatwootEnterpriseConfig:
    inbox_name: str
    widget_color: str


@dataclass(frozen=True)
class EnterpriseConfig:
    schema_version: int
    company: CompanyConfig
    assistant: AssistantConfig
    dify: DifyEnterpriseConfig
    chatwoot: ChatwootEnterpriseConfig
    locale: Literal["zh_CN"]
    business_timezone: Literal["Asia/Shanghai"]

    def to_mapping(self) -> dict[str, object]:
        return asdict(self)


def load_enterprise_config(path: str | Path) -> EnterpriseConfig:
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ValueError("Enterprise configuration could not be read.") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("Enterprise configuration must contain valid JSON.") from exc

    root = _require_mapping(raw, "configuration")
    _require_exact_keys(
        root,
        {
            "schema_version",
            "company",
            "assistant",
            "dify",
            "chatwoot",
            "locale",
            "business_timezone",
        },
        "configuration",
    )
    if isinstance(root["schema_version"], bool) or root["schema_version"] != 1:
        raise ValueError("schema_version must be 1.")

    company = _require_mapping(root["company"], "company")
    _require_exact_keys(company, {"name", "industry", "website_url"}, "company")
    industry = _require_choice(company["industry"], "company.industry", {"ecommerce"})

    assistant = _require_mapping(root["assistant"], "assistant")
    _require_exact_keys(
        assistant,
        {"name", "welcome_title", "welcome_message", "handoff_message"},
        "assistant",
    )

    dify = _require_mapping(root["dify"], "dify")
    _require_exact_keys(
        dify,
        {"dataset_name", "app_name", "indexing_technique"},
        "dify",
    )
    indexing_technique = _require_choice(
        dify["indexing_technique"],
        "dify.indexing_technique",
        {"economy", "high_quality"},
    )

    chatwoot = _require_mapping(root["chatwoot"], "chatwoot")
    _require_exact_keys(chatwoot, {"inbox_name", "widget_color"}, "chatwoot")
    widget_color = _require_text(chatwoot["widget_color"], "chatwoot.widget_color", 7)
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", widget_color) is None:
        raise ValueError("chatwoot.widget_color must be a six-digit hex color.")

    locale = _require_choice(root["locale"], "locale", {"zh_CN"})
    timezone = _require_choice(
        root["business_timezone"],
        "business_timezone",
        {"Asia/Shanghai"},
    )

    return EnterpriseConfig(
        schema_version=1,
        company=CompanyConfig(
            name=_require_text(company["name"], "company.name", 100),
            industry=cast(Literal["ecommerce"], industry),
            website_url=_require_http_url(company["website_url"], "company.website_url"),
        ),
        assistant=AssistantConfig(
            name=_require_text(assistant["name"], "assistant.name", 100),
            welcome_title=_require_text(
                assistant["welcome_title"], "assistant.welcome_title", 100
            ),
            welcome_message=_require_text(
                assistant["welcome_message"], "assistant.welcome_message", 500
            ),
            handoff_message=_require_text(
                assistant["handoff_message"], "assistant.handoff_message", 500
            ),
        ),
        dify=DifyEnterpriseConfig(
            dataset_name=_require_text(dify["dataset_name"], "dify.dataset_name", 100),
            app_name=_require_text(dify["app_name"], "dify.app_name", 100),
            indexing_technique=cast(
                Literal["economy", "high_quality"], indexing_technique
            ),
        ),
        chatwoot=ChatwootEnterpriseConfig(
            inbox_name=_require_text(chatwoot["inbox_name"], "chatwoot.inbox_name", 100),
            widget_color=widget_color.lower(),
        ),
        locale=cast(Literal["zh_CN"], locale),
        business_timezone=cast(Literal["Asia/Shanghai"], timezone),
    )


def _require_mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be an object.")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{path} contains an invalid field name.")
    return cast(Mapping[str, object], value)


def _require_exact_keys(
    value: Mapping[str, object],
    expected: set[str],
    path: str,
) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        prefix = "" if path == "configuration" else f"{path}."
        raise ValueError(f"{prefix}{missing[0]} is required.")
    if unknown:
        prefix = "" if path == "configuration" else f"{path}."
        raise ValueError(f"{prefix}{unknown[0]} is not allowed.")


def _require_text(value: object, path: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be text.")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{path} must contain 1-{maximum} characters.")
    return normalized


def _require_choice(value: object, path: str, choices: set[str]) -> str:
    normalized = _require_text(value, path, 100)
    if normalized not in choices:
        allowed = ", ".join(sorted(choices))
        raise ValueError(f"{path} must be one of: {allowed}.")
    return normalized


def _require_http_url(value: object, path: str) -> str:
    normalized = _require_text(value, path, 2048)
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"{path} must be an http or https URL without credentials.")
    return normalized.rstrip("/")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an enterprise configuration.")
    parser.add_argument("--path", default="config/enterprise.example.json")
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_enterprise_config(args.path)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.print_json:
        # Keep CLI JSON ASCII-only so Windows PowerShell cannot corrupt Unicode
        # while capturing native-process output with a legacy console encoding.
        print(json.dumps(config.to_mapping(), ensure_ascii=True))
    else:
        print("PASS: Enterprise configuration is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
