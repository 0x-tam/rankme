#!/usr/bin/env python3
"""Start, open, enroll, or stop the local RankMe service."""
import argparse
import fcntl
import importlib.util
import json
import os
import secrets
import shlex
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE_URL = 'http://localhost:%s'
HEALTH_URL = 'http://127.0.0.1:%s/api/health'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def probe(port):
    """Read only the public, minimal service identity; never request app state."""
    try:
        with OPENER.open(HEALTH_URL % port, timeout=1.5) as response:
            if response.status != 200 or response.headers.get_content_type() != 'application/json':
                return None
            raw = response.read(4097)
        if len(raw) > 4096:
            return None
        health = json.loads(raw)
        if (not isinstance(health, dict) or health.get('service') != 'rankme'
                or health.get('version') != '1' or not isinstance(health.get('instance'), str)
                or len(health['instance']) < 20 or type(health.get('busy')) is not bool):
            return None
        return health
    except (OSError, ValueError, TypeError, urllib.error.HTTPError):
        return None


def occupied(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def read_record(path):
    """Read private launcher metadata without following an attacker-chosen file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > 4096):
                return None
            value = json.load(stream)
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def write_record(path, value):
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def owned_process(record, health, port, data):
    """Match a private record, current server instance, and exact launched argv."""
    if not record or not health:
        return False
    try:
        pid = record['pid']
        if (type(pid) is not int or pid <= 1 or record['port'] != port
                or record['data_dir'] != str(data) or record['instance'] != health['instance']):
            return False
        command = record['command']
        if (not isinstance(command, list) or len(command) != 6
                or command[1:] != [str(ROOT / 'run.py'), '--port', str(port), '--data-dir', str(data)]
                or not Path(command[0]).is_absolute()):
            return False
        actual = subprocess.check_output(['/bin/ps', '-ww', '-p', str(pid), '-o', 'command='],
                                         text=True, timeout=2).strip()
        return shlex.split(actual) == command
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        return False


def open_page(url, no_open, private=False):
    if not private:
        print('RankMe: ' + url)
    if no_open:
        return True
    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    if not opened:
        if private:
            print('Could not open the enrollment browser. Run again with --enroll; no secret was printed.')
        else:
            print('Open the URL above in your browser.')
    return opened


def stop_server(record_path, record, health, port, data):
    if not health:
        print('No RankMe service is responding on this port.')
        return 0 if not occupied(port) else 1
    if not owned_process(record, health, port, data):
        print('This service was not verified as started by this launcher. Stop it in its original terminal.')
        return 1
    if health['busy']:
        print('A job is running. Pause automation in Settings, wait for completion, then stop RankMe.')
        return 1
    pid = record['pid']
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print('The recorded RankMe process already stopped.')
        return 1
    for _ in range(30):
        time.sleep(0.1)
        if not probe(port):
            record_path.unlink(missing_ok=True)
            print('RankMe stopped. Local data is preserved.')
            return 0
    print('Shutdown requested. Wait a moment before restarting.')
    return 0


def enroll(data, port):
    # The fragment stays out of HTTP; never print it or pass it to the server.
    try:
        from rankme.auth import issue_bootstrap
        secret = issue_bootstrap(data)
    except Exception:
        print('Owner enrollment is unavailable. If a passkey is already enrolled, sign in normally. '
              'For lost keys, use the explicit local recovery command in README.md.', file=sys.stderr)
        return 1
    if not open_page(BASE_URL % port + '/#bootstrap=' + secret, False, private=True):
        return 1
    print('Complete owner passkey enrollment in the browser.')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Launch RankMe locally and open its dashboard')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--data-dir', default=str(ROOT / 'data'))
    parser.add_argument('--no-open', action='store_true', help='Start without opening a browser')
    parser.add_argument('--enroll', action='store_true', help='Explicitly start first-owner passkey enrollment')
    parser.add_argument('--stop', action='store_true', help='Stop an idle service started by this launcher')
    parser.add_argument('--status', action='store_true', help='Show whether RankMe is running')
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10):
        parser.error('Python 3.10 or later is required; Python 3.12 is recommended')
    if not 1024 <= args.port <= 65535:
        parser.error('Port must be between 1024 and 65535')
    if sum((args.enroll, args.stop, args.status)) > 1 or (args.enroll and args.no_open):
        parser.error('--enroll requires opening a browser and cannot be combined with --stop, --status, or --no-open')
    data = Path(args.data_dir).expanduser().resolve()
    data.mkdir(parents=True, exist_ok=True)
    data.chmod(0o700)
    record_path = data / ('launcher-%s.json' % args.port)
    lock_fd = os.open(data / 'launcher.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'w') as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            print('Unsafe launcher lock file.', file=sys.stderr)
            return 1
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        health = probe(args.port)
        record = read_record(record_path)
        own = owned_process(record, health, args.port, data)
        if args.stop:
            return stop_server(record_path, record, health, args.port, data)
        if args.status:
            if own:
                print('RankMe is running.' + (' A job is active.' if health['busy'] else ''))
                return 0
            print('No launcher-owned RankMe service is running on this port.')
            return 1
        if health or occupied(args.port):
            if not own:
                print('Port %s is occupied by a service this launcher cannot verify. Choose another --port '
                      'or stop the service in its original terminal.' % args.port, file=sys.stderr)
                return 1
            if args.enroll:
                return enroll(data, args.port)
            print('Using the existing RankMe service on this port.')
            open_page(BASE_URL % args.port, args.no_open)
            return 0
        if importlib.util.find_spec('fido2') is None:
            print('RankMe dependencies are missing. Run: .venv/bin/python -m pip install -r requirements.txt '
                  '(create .venv with python3.12 -m venv .venv first).', file=sys.stderr)
            return 1
        env = dict(os.environ)
        extra = [str(Path.home() / '.local/bin'), '/opt/homebrew/bin', '/usr/local/bin']
        env['PATH'] = os.pathsep.join(extra + [env.get('PATH', '/usr/bin:/bin')])
        env['PYTHONUNBUFFERED'] = '1'
        command = [sys.executable, str(ROOT / 'run.py'), '--port', str(args.port), '--data-dir', str(data)]
        log_path = data / 'server.log'
        with log_path.open('ab') as log:
            process = subprocess.Popen(command, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, start_new_session=True,
                                       close_fds=True, env=env)
        for _ in range(80):
            if process.poll() is not None:
                print('RankMe did not start. Inspect ' + str(log_path), file=sys.stderr)
                return 1
            health = probe(args.port)
            if health:
                record = {'pid': process.pid, 'port': args.port, 'data_dir': str(data),
                          'instance': health['instance'], 'command': command,
                          'started_at': datetime.now(timezone.utc).isoformat()}
                write_record(record_path, record)
                if not owned_process(record, health, args.port, data):
                    print('RankMe started, but process identity could not be verified. Inspect ' + str(log_path),
                          file=sys.stderr)
                    return 1
                if args.enroll:
                    return enroll(data, args.port)
                open_page(BASE_URL % args.port, args.no_open)
                print('Runs in the background while this computer is awake. Closing the browser does not stop it.')
                return 0
            time.sleep(0.15)
        process.terminate()
        print('RankMe startup timed out. Inspect ' + str(log_path), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
