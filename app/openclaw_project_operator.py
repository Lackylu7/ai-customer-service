from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
_PREFIX_PATTERN = re.compile(r"^\s*龙虾\s*[：:]\s*(?P<command>.+?)\s*$")
_UPGRADE_PATTERN = re.compile(
    r"^升级机器人(?:到|至|\s+)?(?P<version>v\d+\.\d+(?:\.\d+)?)$",
    re.IGNORECASE,
)

_FIXED_COMMANDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("检查机器人状态", "查看机器人状态", "项目状态", "服务状态"), "status"),
    (("项目概况", "项目摘要", "运营摘要", "今日概况", "生成项目报告"), "summary"),
    (("查看最近错误", "查看最近报错", "最近错误", "错误日志", "检查日志"), "errors"),
    (("运行机器人测试", "运行回归测试", "检查问答链路", "验收机器人"), "test"),
    (("检查知识库", "知识库状态", "查看知识状态", "知识覆盖情况"), "knowledge-status"),
    (("查看业务路由", "负责人路由", "业务路由", "查看负责人配置"), "routes"),
    (("备份机器人", "备份项目", "创建项目备份"), "backup"),
)


@dataclass(frozen=True)
class ProjectOperatorReply:
    text: str


class OpenClawProjectOperator:
    """Expose a small, allowlisted project-operations surface in DingTalk."""

    def __init__(
        self,
        *,
        operator_user_ids: Sequence[str],
        project_root: Path,
        runner: CommandRunner = subprocess.run,
        timeout_seconds: int = 20,
    ) -> None:
        self._operator_user_ids = frozenset(
            user_id.strip() for user_id in operator_user_ids if user_id.strip()
        )
        self._project_root = project_root.resolve()
        self._runner = runner
        self._timeout_seconds = timeout_seconds

    def handle(
        self,
        *,
        sender_id: str,
        text: str,
        is_direct: bool,
    ) -> ProjectOperatorReply | None:
        match = _PREFIX_PATTERN.match(text)
        if match is None:
            return None
        if not is_direct:
            return ProjectOperatorReply("项目运维指令请私聊机器人使用。")
        if sender_id not in self._operator_user_ids:
            return ProjectOperatorReply("你没有项目运维权限。")

        command = match.group("command").strip()
        if command in {"检查机器人状态", "查看机器人状态", "项目状态"}:
            return self._run_control("status")

        upgrade_match = _UPGRADE_PATTERN.match(command)
        if upgrade_match is not None:
            return self._run_control(
                "queue-upgrade",
                upgrade_match.group("version").lower(),
            )

        normalized = re.sub(r"[，。！？!?\s]+", "", command)
        if normalized in {"帮助", "项目管家帮助", "你能做什么"}:
            return ProjectOperatorReply(self._help_text())
        for phrases, control_command in _FIXED_COMMANDS:
            if any(normalized == re.sub(r"\s+", "", phrase) for phrase in phrases):
                return self._run_control(control_command)

        if "状态" in normalized and any(
            word in normalized for word in ("机器人", "项目", "服务", "Dify")
        ):
            return self._run_control("status")
        if any(word in normalized for word in ("报错", "错误日志", "最近错误")):
            return self._run_control("errors")
        if "知识库" in normalized and any(
            word in normalized for word in ("检查", "状态", "覆盖", "情况")
        ):
            return self._run_control("knowledge-status")
        if any(word in normalized for word in ("回归测试", "验收测试", "问答链路")):
            return self._run_control("test")

        return ProjectOperatorReply(
            self._help_text()
        )

    @staticmethod
    def _help_text() -> str:
        return (
            "龙虾项目管家目前支持：\n"
            "• 检查机器人状态 / 项目概况\n"
            "• 查看最近错误 / 运行机器人测试\n"
            "• 检查知识库 / 查看业务路由\n"
            "• 备份机器人\n"
            "• 升级机器人 v0.10\n"
            "请私聊并以“龙虾：”开头。升级只读取 Mac 下载或桌面目录中的指定安装包。"
        )

    def _run_control(self, *arguments: str) -> ProjectOperatorReply:
        script = self._project_root / "scripts" / "macos" / "project-control.sh"
        if not script.is_file():
            return ProjectOperatorReply("项目运维工具尚未安装完整。")
        try:
            completed = self._runner(
                ["bash", str(script), *arguments],
                cwd=str(self._project_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ProjectOperatorReply("项目运维工具暂时不可用，请稍后重试。")
        output = (completed.stdout or completed.stderr).strip()
        if completed.returncode != 0:
            return ProjectOperatorReply(output or "项目运维操作未成功。")
        return ProjectOperatorReply(output or "项目运维操作已受理。")
