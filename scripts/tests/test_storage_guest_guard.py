"""A test-only guest helper must never power off the developer's host."""
import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import Mock, patch


class GuestGuard(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1]/getattr(self, "script", "validate-storage-runtime-guest.py")
        spec = importlib.util.spec_from_file_location("storage_guest_under_test", path)
        self.module = importlib.util.module_from_spec(spec)
        with patch("subprocess.run") as command:
            spec.loader.exec_module(self.module)
            command.assert_not_called()

    def test_import_and_nonroot_execution_never_shutdown(self):
        with patch.object(self.module.os, "geteuid", return_value=1000), \
                patch.object(self.module.subprocess, "run") as command, \
                patch.object(self.module, "main") as main:
            with self.assertRaises(SystemExit):
                self.module.run_guest()
            command.assert_not_called()
            main.assert_not_called()

    def test_root_without_marker_never_shutdown(self):
        marker = Mock()
        marker.is_file.return_value = False
        with patch.object(self.module.os, "geteuid", return_value=0), \
                patch.object(self.module, "P", return_value=marker), \
                patch.object(self.module.subprocess, "run") as command:
            with self.assertRaises(SystemExit):
                self.module.run_guest()
            command.assert_not_called()

    def test_wrong_marker_never_shutdown(self):
        marker = Mock()
        marker.is_file.return_value = True
        marker.read_text.return_value = "ordinary-host\n"
        with patch.object(self.module.os, "geteuid", return_value=0), \
                patch.object(self.module, "P", return_value=marker), \
                patch.object(self.module.subprocess, "run") as command:
            with self.assertRaises(SystemExit):
                self.module.run_guest()
            command.assert_not_called()

    def test_marked_guest_shuts_down_after_success_or_failure(self):
        marker = Mock()
        marker.is_file.return_value = True
        marker.read_text.return_value = "disposable-vm\n"
        for failure in (None, RuntimeError("injected test failure")):
            with self.subTest(failure=failure), \
                    patch.object(self.module.os, "geteuid", return_value=0), \
                    patch.object(self.module, "P", return_value=marker), \
                    patch.object(self.module, "main", side_effect=failure) as main, \
                    patch.object(self.module, "OUT") as out, \
                    patch.object(self.module.subprocess, "run") as command, \
                    contextlib.redirect_stdout(io.StringIO()):
                out.is_mount.return_value = False
                self.module.run_guest()
                main.assert_called_once()
                command.assert_called_once_with(["systemctl", "poweroff"], check=False)


class PR4CloseGuestGuard(GuestGuard):
    script = "validate-pr4-close-guest.py"


if __name__ == "__main__":
    unittest.main()


class Phase2FinalGuestGuard(GuestGuard):
    script = "validate-phase2-final-guest.py"


class Phase3GuestGuard(GuestGuard):
    script = "validate-phase3-guest.py"
