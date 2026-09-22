"""Credential isolation, connection controls, and installed CLI behavior."""
import contextlib
import getpass
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import cli
import credentials
import providers
from test_router import LAYA_RESPONSE, fake_request


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root), "TYPESAFE_API_KEY": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_saved_key_used_by_provider_and_removed(self):
        credentials.save_jev_key("test-secret")
        self.assertEqual(credentials.key_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(credentials.key_path().parent.stat().st_mode & 0o777, 0o700)
        request = fake_request()
        providers.Classifier(request).predict("jev", "Example", "en")
        self.assertEqual(request.calls[0]["token"], "test-secret")
        credentials.remove_jev_key()
        self.assertIsNone(credentials.read_jev_key())
        credentials.remove_jev_key()

    def test_environment_wins_without_changing_saved_key(self):
        credentials.save_jev_key("saved")
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "environment"}):
            self.assertEqual(credentials.read_jev_key(), "environment")
        self.assertEqual(credentials.read_jev_key(), "saved")

    def test_rejects_unsafe_or_invalid_saved_key(self):
        credentials.save_jev_key("original")
        for invalid in ("", "two words", "x\ny", "x" * 4097):
            with self.assertRaises(ValueError):
                credentials.save_jev_key(invalid)
        self.assertEqual(credentials.read_jev_key(), "original")
        credentials.key_path().chmod(0o644)
        with self.assertRaises(ValueError):
            credentials.read_jev_key()
        credentials.key_path().unlink()
        target = self.root / "elsewhere"
        target.write_text("private")
        credentials.key_path().symlink_to(target)
        with self.assertRaises(ValueError):
            credentials.read_jev_key()
        credentials.save_jev_key("replacement")
        self.assertEqual(target.read_text(), "private")
        self.assertFalse(credentials.key_path().is_symlink())

    def test_hidden_configuration_never_prints_key(self):
        output = io.StringIO()
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("getpass.getpass", return_value="test-secret"), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["configure", "jev"]), 0)
        self.assertNotIn("test-secret", output.getvalue())
        self.assertEqual(credentials.read_jev_key(), "test-secret")

    def test_noninteractive_or_echoing_input_is_refused(self):
        with mock.patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["configure", "jev"]), 2)
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("getpass.getpass", side_effect=getpass.GetPassWarning), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["configure", "jev"]), 2)
        self.assertIsNone(credentials.read_jev_key())

    def test_probe_calls_jev_once_without_routes_or_launch(self):
        credentials.save_jev_key("test-secret")
        request = fake_request(LAYA_RESPONSE)
        with mock.patch("cli.Classifier", return_value=providers.Classifier(request)), mock.patch("cli.launch") as launch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["health", "--backend", "jev", "--probe"]), 0)
        self.assertEqual(len(request.calls), 1)
        self.assertEqual(request.calls[0]["body"]["state"], {"request": "Explain what a Python list is."})
        launch.assert_not_called()

    def test_invalid_key_probe_returns_failure(self):
        with mock.patch("cli.Classifier") as classifier, contextlib.redirect_stdout(io.StringIO()):
            classifier.return_value.predict.side_effect = providers.ProviderError("Classifier returned HTTP 401")
            self.assertEqual(cli.main(["health", "--backend", "jev", "--probe"]), 1)

    def test_symlinked_launcher_finds_cli(self):
        link = self.root / "agent-router"
        link.symlink_to(Path(__file__).resolve().parent / "agent-router")
        result = subprocess.run([str(link), "configure", "--help"], capture_output=True, text=True, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--remove", result.stdout)


if __name__ == "__main__":
    unittest.main()
