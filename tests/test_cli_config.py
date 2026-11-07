import os
import subprocess
import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class CliConfigTests(unittest.TestCase):
    def run_python(self, code, extra_environment=None):
        environment = os.environ.copy()
        environment.pop("USER_FILL_INACTIVITY_RECONNECT_SECONDS", None)
        environment.pop("WEBSOCKET_ACTIVITY_TIMEOUT", None)
        if extra_environment:
            environment.update(extra_environment)
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )

    def test_user_fill_timeout_uses_elapsed_fill_time(self):
        result = self.run_python(
            "from src.cli_app import user_fill_inactivity_timed_out; "
            "assert not user_fill_inactivity_timed_out(100, 900, 999); "
            "assert user_fill_inactivity_timed_out(100, 900, 1000.1)"
        )
        self.assertEqual(result.stdout, "")

    def test_new_timeout_setting_takes_precedence_over_legacy_setting(self):
        result = self.run_python(
            "import src.cli_app as app; print(app.USER_FILL_INACTIVITY_RECONNECT_SECONDS)",
            {
                "USER_FILL_INACTIVITY_RECONNECT_SECONDS": "120",
                "WEBSOCKET_ACTIVITY_TIMEOUT": "240",
            },
        )
        self.assertEqual(result.stdout.strip(), "120")

    def test_legacy_timeout_setting_remains_supported(self):
        result = self.run_python(
            "import src.cli_app as app; print(app.USER_FILL_INACTIVITY_RECONNECT_SECONDS)",
            {"WEBSOCKET_ACTIVITY_TIMEOUT": "240"},
        )
        self.assertEqual(result.stdout.strip(), "240")

    def test_log_levels_are_routed_to_the_expected_stream(self):
        result = self.run_python(
            "import logging; logging.basicConfig(format='legacy %(message)s'); "
            "import src.cli_app; "
            "logger = logging.getLogger('hdm.test'); "
            "logger.info('info-marker'); logger.warning('warning-marker'); "
            "logger.error('error-marker')",
        )
        self.assertIn("info-marker", result.stdout)
        self.assertNotIn("info-marker", result.stderr)
        self.assertNotIn("warning-marker", result.stdout)
        self.assertIn("warning-marker", result.stderr)
        self.assertIn("error-marker", result.stderr)
        self.assertNotIn("error-marker", result.stdout)
        self.assertNotIn("legacy", result.stdout)
        self.assertNotIn("legacy", result.stderr)
        self.assertIn("container=", result.stdout)
        self.assertIn("pid=", result.stdout)
        self.assertIn("container=", result.stderr)
        self.assertIn("pid=", result.stderr)


if __name__ == "__main__":
    unittest.main()
