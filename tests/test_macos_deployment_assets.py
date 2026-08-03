import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MACOS_SCRIPTS = ROOT / "scripts" / "macos"


class MacOSDeploymentAssetTests(unittest.TestCase):
    def test_scripts_exist_and_use_strict_shell_mode(self) -> None:
        for name in (
            "host-preflight.sh",
            "preflight.sh",
            "setup-dify.sh",
            "run-dify.sh",
            "run-dingtalk-stream.sh",
            "install-openclaw-agent.sh",
            "install-openclaw-companion-agent.sh",
            "install-openclaw-project-manager-skill.sh",
            "enable-openclaw-shareholder.sh",
            "install-launch-agent.sh",
            "install-or-upgrade.sh",
            "project-control.sh",
            "run-pending-upgrade.sh",
            "install-project-operator.sh",
        ):
            with self.subTest(name=name):
                content = (MACOS_SCRIPTS / name).read_text(encoding="utf-8")
                self.assertTrue(content.startswith("#!/bin/bash\nset -eu\n"))

        importer = (MACOS_SCRIPTS / "import-runtime-config.js").read_text(
            encoding="utf-8"
        )
        self.assertTrue(importer.startswith("#!/usr/bin/env node\n"))
        state_importer = (MACOS_SCRIPTS / "import-state-database.js").read_text(
            encoding="utf-8"
        )
        self.assertTrue(state_importer.startswith("#!/usr/bin/env node\n"))

    def test_one_step_upgrade_preserves_runtime_state(self) -> None:
        content = (MACOS_SCRIPTS / "install-or-upgrade.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("deployment-manifest.json", content)
        self.assertIn(".runtime/deployment-backups", content)
        self.assertIn("SERVICE_WAS_RUNNING", content)
        self.assertIn("launchctl bootout", content)
        self.assertIn("launchctl bootstrap", content)
        self.assertIn("rollback_on_error", content)
        self.assertIn("restoring the previous application code", content)
        self.assertIn("-m ensurepip --upgrade", content)
        self.assertNotIn("rsync -a --delete", content)
        for preserved in (
            "--exclude '.env'",
            "--exclude '.runtime/'",
            "--exclude '.venv/'",
            "--exclude 'config/enterprise.local.json'",
            "--exclude 'config/store-directory.local.json'",
        ):
            self.assertIn(preserved, content)

        launcher = (ROOT / "upgrade-macos.command").read_text(encoding="utf-8")
        self.assertTrue(launcher.startswith("#!/bin/bash\nset -eu\n"))
        self.assertIn("scripts/macos/install-or-upgrade.sh", launcher)

        openclaw_launcher = (
            ROOT / "upgrade-and-enable-openclaw.command"
        ).read_text(encoding="utf-8")
        self.assertTrue(openclaw_launcher.startswith("#!/bin/bash\nset -eu\n"))
        self.assertIn("scripts/macos/install-or-upgrade.sh", openclaw_launcher)
        self.assertIn(
            "scripts/macos/enable-openclaw-shareholder.sh",
            openclaw_launcher,
        )
        self.assertIn("WorkingDirectory", openclaw_launcher)

    def test_openclaw_activation_backs_up_config_and_can_rollback(self) -> None:
        content = (
            MACOS_SCRIPTS / "enable-openclaw-shareholder.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(".env-before-openclaw-", content)
        self.assertIn("rollback_on_error", content)
        self.assertIn('OPENCLAW_SHAREHOLDER_ENABLED": "true"', content)
        self.assertIn('OPENCLAW_PROJECT_OPERATOR_ENABLED": "true"', content)
        self.assertIn('OPENCLAW_GENERAL_CONVERSATION_ENABLED": "true"', content)
        self.assertIn("install-openclaw-companion-agent.sh", content)
        self.assertIn("install-openclaw-project-manager-skill.sh", content)
        self.assertIn("install-project-operator.sh", content)
        self.assertIn("launchctl kickstart -k", content)
        self.assertIn("state = running", content)

    def test_project_operator_allows_only_versioned_packages_and_fixed_actions(self):
        control = (MACOS_SCRIPTS / "project-control.sh").read_text(
            encoding="utf-8"
        )
        updater = (MACOS_SCRIPTS / "run-pending-upgrade.sh").read_text(
            encoding="utf-8"
        )
        installer = (MACOS_SCRIPTS / "install-project-operator.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("queue-upgrade", control)
        self.assertIn("deployment-manifest.json", control)
        self.assertIn("shasum -a 256", control)
        self.assertIn('case "${1:-}"', control)
        self.assertIn("nohup launchctl kickstart", control)
        self.assertIn("project-updater.kickstart.log", control)
        for fixed_command in (
            "summary",
            "errors",
            "test",
            "knowledge-status",
            "routes",
            "backup",
        ):
            self.assertIn(fixed_command, control)
        self.assertIn('os.chmod(target, 0o600)\nPY\n  mv "$TEMPORARY"', control)
        self.assertNotIn("eval \"$2\"", control)
        self.assertIn("EXPECTED_HASH", updater)
        self.assertIn("upgrade-and-enable-openclaw.command", updater)
        self.assertIn("upgrade.lock", updater)
        self.assertIn("com.shenhuo.dingtalk-updater", installer)

    def test_project_manager_skill_uses_only_the_controlled_entrypoint(self):
        content = (
            ROOT / "openclaw" / "project-manager-skill" / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("scripts/macos/project-control.sh", content)
        self.assertIn("不把用户自然语言直接拼接进 Shell", content)
        self.assertNotIn("rm -rf", content)

    def test_openclaw_installer_does_not_bind_or_deliver(self) -> None:
        content = (MACOS_SCRIPTS / "install-openclaw-agent.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("--bind", content)
        self.assertNotIn("--deliver", content)
        self.assertIn('"\\\\" in name', content)
        self.assertIn('".." in path.parts', content)

    def test_launch_agent_is_prepared_without_automatic_start(self) -> None:
        content = (MACOS_SCRIPTS / "install-launch-agent.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('"KeepAlive": True', content)
        self.assertIn('"RunAtLoad": True', content)
        self.assertNotIn("launchctl bootstrap gui/", content.split("PY\n", 1)[0])

    def test_preflight_checks_dify_reachability_without_printing_secrets(self) -> None:
        content = (MACOS_SCRIPTS / "preflight.sh").read_text(encoding="utf-8")
        self.assertIn("Dify endpoint is reachable from this Mac", content)
        self.assertIn("values were not printed", content)
        self.assertNotIn("DIFY_APP_API_KEY]", content)

    def test_runtime_requirements_are_minimal_and_pinned(self) -> None:
        requirements = (ROOT / "requirements-dingtalk.txt").read_text(
            encoding="utf-8"
        ).strip().splitlines()
        self.assertEqual(requirements, ["dingtalk-stream==0.24.3"])

    def test_host_preflight_is_read_only_and_reports_key_ports(self) -> None:
        content = (MACOS_SCRIPTS / "host-preflight.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("No services were changed", content)
        self.assertIn("8081 8443 18789 18791", content)
        self.assertIn("sys.version_info >= (3, 10)", content)
        self.assertIn("Python 3.10+ is required", content)
        self.assertNotIn("colima start\n", content)

    def test_dify_setup_is_pinned_and_does_not_start_by_default(self) -> None:
        content = (MACOS_SCRIPTS / "setup-dify.sh").read_text(encoding="utf-8")
        self.assertIn('VERSION="${DIFY_VERSION:-1.15.0}"', content)
        self.assertIn('START_AFTER_SETUP=0', content)
        self.assertIn('if [ "$START_AFTER_SETUP" -eq 1 ]', content)
        self.assertIn('chmod 600 "$ENV_PATH"', content)
        self.assertIn('127.0.0.1:$HTTP_PORT', content)
        self.assertIn('curl --http1.1 --fail --location', content)
        self.assertIn('--retry-all-errors', content)
        self.assertIn('mv "$EXTRACTED_ROOT" "$DIFY_ROOT"', content)
        self.assertNotIn("git clone", content)
        self.assertNotIn("docker compose up -d", content)

    def test_dify_runner_exposes_only_expected_actions(self) -> None:
        content = (MACOS_SCRIPTS / "run-dify.sh").read_text(encoding="utf-8")
        self.assertIn("start|status|stop|logs", content)
        self.assertIn("compose up -d", content)
        self.assertIn("compose down", content)
        self.assertNotIn("down -v", content)

    def test_runtime_importer_authenticates_and_writes_secrets_atomically(self) -> None:
        content = (MACOS_SCRIPTS / "import-runtime-config.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('"aes-256-gcm"', content)
        self.assertIn('oaepHash: "sha256"', content)
        self.assertIn("decipher.setAuthTag", content)
        self.assertIn("fs.renameSync(temporaryPath, filePath)", content)
        self.assertIn("plaintext.fill(0)", content)
        self.assertIn("no secret values were printed", content)

    def test_state_importer_authenticates_and_refuses_live_sidecars(self) -> None:
        content = (MACOS_SCRIPTS / "import-state-database.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('envelope.purpose !== "dingtalk-state-database"', content)
        self.assertIn("decipher.setAuthTag", content)
        self.assertIn("plaintext_sha256", content)
        self.assertIn("database sidecar files exist", content)
        self.assertIn("fs.renameSync(temporaryPath, targetPath)", content)
        self.assertIn("no message content was printed", content)


if __name__ == "__main__":
    unittest.main()
