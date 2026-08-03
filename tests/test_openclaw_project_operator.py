import subprocess
import tempfile
import unittest
from pathlib import Path

from app.openclaw_project_operator import OpenClawProjectOperator


class RecordingRunner:
    def __init__(self, *, stdout="OK", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            self.returncode,
            stdout=self.stdout,
            stderr="",
        )


class OpenClawProjectOperatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        script = self.root / "scripts" / "macos" / "project-control.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/bash\n", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_ignores_normal_customer_questions(self):
        runner = RecordingRunner()
        operator = OpenClawProjectOperator(
            operator_user_ids=("operator-1",),
            project_root=self.root,
            runner=runner,
        )

        result = operator.handle(
            sender_id="operator-1",
            text="公司有哪些股东类型？",
            is_direct=True,
        )

        self.assertIsNone(result)
        self.assertEqual(runner.calls, [])

    def test_rejects_non_operator_and_group_commands(self):
        operator = OpenClawProjectOperator(
            operator_user_ids=("operator-1",),
            project_root=self.root,
        )

        denied = operator.handle(
            sender_id="employee-1",
            text="龙虾：检查机器人状态",
            is_direct=True,
        )
        group = operator.handle(
            sender_id="operator-1",
            text="龙虾：检查机器人状态",
            is_direct=False,
        )

        self.assertIn("没有", denied.text)
        self.assertIn("私聊", group.text)

    def test_runs_only_allowlisted_status_command(self):
        runner = RecordingRunner(stdout="服务运行正常")
        operator = OpenClawProjectOperator(
            operator_user_ids=("operator-1",),
            project_root=self.root,
            runner=runner,
        )

        result = operator.handle(
            sender_id="operator-1",
            text="龙虾：检查机器人状态",
            is_direct=True,
        )

        self.assertEqual(result.text, "服务运行正常")
        self.assertEqual(runner.calls[0][0][-1], "status")

    def test_queues_an_explicit_version_without_accepting_shell_text(self):
        runner = RecordingRunner(stdout="升级任务已受理：v0.8")
        operator = OpenClawProjectOperator(
            operator_user_ids=("operator-1",),
            project_root=self.root,
            runner=runner,
        )

        result = operator.handle(
            sender_id="operator-1",
            text="龙虾：升级机器人 v0.8",
            is_direct=True,
        )
        unsupported = operator.handle(
            sender_id="operator-1",
            text="龙虾：升级机器人 v0.8; rm -rf /",
            is_direct=True,
        )

        self.assertIn("已受理", result.text)
        self.assertEqual(runner.calls[0][0][-2:], ["queue-upgrade", "v0.8"])
        self.assertIn("目前支持", unsupported.text)
        self.assertEqual(len(runner.calls), 1)

    def test_routes_natural_project_manager_commands_to_fixed_actions(self):
        runner = RecordingRunner(stdout="完成")
        operator = OpenClawProjectOperator(
            operator_user_ids=("operator-1",),
            project_root=self.root,
            runner=runner,
        )

        commands = {
            "龙虾：项目概况": "summary",
            "龙虾：帮我看看最近有没有报错": "errors",
            "龙虾：知识库状态": "knowledge-status",
            "龙虾：检查问答链路": "test",
            "龙虾：查看业务路由": "routes",
            "龙虾：备份机器人": "backup",
        }
        for text, expected in commands.items():
            with self.subTest(text=text):
                result = operator.handle(
                    sender_id="operator-1",
                    text=text,
                    is_direct=True,
                )
                self.assertEqual(result.text, "完成")
                self.assertEqual(runner.calls[-1][0][-1], expected)

    def test_help_lists_the_safe_project_manager_surface(self):
        operator = OpenClawProjectOperator(
            operator_user_ids=("operator-1",),
            project_root=self.root,
        )

        result = operator.handle(
            sender_id="operator-1",
            text="龙虾：帮助",
            is_direct=True,
        )

        self.assertIn("项目概况", result.text)
        self.assertIn("检查知识库", result.text)
        self.assertIn("升级机器人", result.text)


if __name__ == "__main__":
    unittest.main()
