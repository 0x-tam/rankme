"""The launcher must identify its own process without reading app data."""
import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'launch.py'
SPEC = importlib.util.spec_from_file_location('rankme_launcher', SCRIPT)
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_probe_uses_only_public_health(self):
        class Response:
            status = 200
            headers = type('Headers', (), {'get_content_type': lambda self: 'application/json'})()

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                pass

            def read(self, maximum):
                self_max = maximum
                assert self_max == 4097
                return b'{"service":"rankme","version":"1","instance":"123456789012345678901234","busy":false}'

        urls = []
        with patch.object(launcher.OPENER, 'open', side_effect=lambda url, timeout: urls.append(url) or Response()):
            self.assertFalse(launcher.probe(18787)['busy'])
        self.assertEqual(urls, ['http://127.0.0.1:18787/api/health'])

    def test_private_record_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'launcher.json'
            record = {'pid': 123, 'instance': 'x' * 32}
            launcher.write_record(path, record)
            self.assertEqual(launcher.read_record(path), record)
            path.chmod(0o644)
            self.assertIsNone(launcher.read_record(path))
            path.unlink()
            path.symlink_to(Path(directory) / 'other.json')
            self.assertIsNone(launcher.read_record(path))

    def test_process_check_requires_record_instance_and_exact_command(self):
        data = Path('/tmp/rankme-launcher-test')
        command = ['/tmp/python', str(launcher.ROOT / 'run.py'), '--port', '8787', '--data-dir', str(data)]
        record = {'pid': 123, 'port': 8787, 'data_dir': str(data), 'instance': 'i' * 32, 'command': command}
        health = {'instance': 'i' * 32, 'busy': False}
        with patch.object(launcher.subprocess, 'check_output', return_value=' '.join(command)):
            self.assertTrue(launcher.owned_process(record, health, 8787, data))
            self.assertFalse(launcher.owned_process(record, {**health, 'instance': 'j' * 32}, 8787, data))
            self.assertFalse(launcher.owned_process({**record, 'data_dir': '/tmp/other'}, health, 8787, data))

    def test_busy_service_is_not_signaled(self):
        with patch.object(launcher, 'owned_process', return_value=True), patch.object(launcher.os, 'kill') as kill:
            with contextlib.redirect_stdout(io.StringIO()):
                result = launcher.stop_server(Path('/tmp/no-record'), {'pid': 123}, {'busy': True}, 8787, Path('/tmp'))
        self.assertEqual(result, 1)
        kill.assert_not_called()

    def test_enrollment_secret_is_only_sent_to_browser_fragment(self):
        import sys
        from types import ModuleType
        fake_auth = ModuleType('rankme.auth')
        fake_auth.issue_bootstrap = lambda data: 'private-invitation'
        urls = []
        output = io.StringIO()
        with patch.dict(sys.modules, {'rankme.auth': fake_auth}), patch.object(launcher.webbrowser, 'open', side_effect=lambda url: urls.append(url) or True):
            with contextlib.redirect_stdout(output):
                result = launcher.enroll(Path('/tmp/rankme-test'), 8787)
        self.assertEqual(result, 0)
        self.assertEqual(urls, ['http://localhost:8787/#bootstrap=private-invitation'])
        self.assertNotIn('private-invitation', output.getvalue())


if __name__ == '__main__':
    unittest.main()
