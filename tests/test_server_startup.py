"""A second worker must never run provider recovery against the active store."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rankme.server import acquire_data_lock, main, Application


class StartupTests(unittest.TestCase):
    def test_shared_directory_is_rejected_before_lock_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory).chmod(0o770)
            with self.assertRaises(ValueError): acquire_data_lock(directory)
            self.assertFalse((Path(directory) / 'server.lock').exists())

    def test_recovery_uses_same_lock_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('rankme.server.sys.stdin.isatty', return_value=True), \
                    patch('rankme.server.sys.stdout.isatty', return_value=True), \
                    patch('rankme.server.acquire_data_lock', side_effect=ValueError('unsafe')) as acquire, \
                    patch('rankme.server.reset_for_recovery') as reset, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(['--recover-passkeys', '--data-dir', directory])
                acquire.assert_called_once_with(Path(directory).resolve())
                reset.assert_not_called()

    def test_second_start_does_not_construct_application(self):
        with tempfile.TemporaryDirectory() as directory, acquire_data_lock(directory):
            with patch('rankme.server.Application') as app, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--data-dir', directory, '--no-worker']), 1)
                app.assert_not_called()

    def test_legacy_readable_lock_is_tightened_without_replacing_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'server.lock'
            path.write_text('')
            path.chmod(0o644)
            inode = path.stat().st_ino
            with acquire_data_lock(directory):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.stat().st_ino, inode)

    def test_lock_refuses_symlinks_hardlinks_and_shared_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'other'
            target.write_text('unchanged')
            target.chmod(0o600)
            lock = root / 'server.lock'
            lock.symlink_to(target)
            with self.assertRaises(OSError): acquire_data_lock(directory)
            lock.unlink()
            os.link(target, lock)
            with self.assertRaises(ValueError): acquire_data_lock(directory)
            lock.unlink()
            lock.write_text('')
            lock.chmod(0o666)
            with self.assertRaises(ValueError): acquire_data_lock(directory)
            self.assertEqual(target.read_text(), 'unchanged')

    def test_background_engine_start_is_once_and_needs_no_browser_session(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Application(directory)
            try:
                with patch.object(app.engine, 'start') as start:
                    app.start_engine()
                    app.start_engine()
                    start.assert_called_once()
            finally:
                app.store.close()
