"""Restricted one-shot answer command used by the DingTalk Stream connector."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from app.dify_chat_client import DifyChatAPIError, DifyChatClient
from app.enterprise_assistant import AssistantBrainProvider, EnterpriseAssistant


_UNAVAILABLE_TEXT = "服务暂时不可用，请稍后重试或联系对应负责人。"
_MVP_USER = "dingtalk:mvp-test-user"


def load_env_file(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE entries without overwriting process configuration."""

    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        normalized_value = value.strip()
        if (
            len(normalized_value) >= 2
            and normalized_value[0] == normalized_value[-1]
            and normalized_value[0] in {"'", '"'}
        ):
            normalized_value = normalized_value[1:-1]
        if normalized_key and normalized_key not in os.environ:
            os.environ[normalized_key] = normalized_value


def build_brain(environ: Mapping[str, str] | None = None) -> DifyChatClient:
    source = os.environ if environ is None else environ
    base_url = source.get("DIFY_BASE_URL", "http://127.0.0.1:8081").strip()
    api_key = source.get("DIFY_APP_API_KEY", "").strip()
    return DifyChatClient(base_url=base_url, api_key=api_key, timeout=60)


def run_question(
    question: str,
    *,
    brain: AssistantBrainProvider,
) -> str:
    """Answer as a group-channel MVP so the strictest policy is always applied."""

    try:
        result = EnterpriseAssistant(brain=brain).answer(
            question,
            user=_MVP_USER,
            channel="group",
        )
    except (DifyChatAPIError, OSError, ValueError):
        return _UNAVAILABLE_TEXT
    return result.text


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    question = " ".join(arguments).strip()
    if not question:
        print(_UNAVAILABLE_TEXT)
        return 2

    load_env_file()
    try:
        brain = build_brain()
    except ValueError:
        print(_UNAVAILABLE_TEXT)
        return 2
    print(run_question(question, brain=brain))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
