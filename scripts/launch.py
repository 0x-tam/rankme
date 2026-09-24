#!/usr/bin/env python3
"""Start, open, or stop the local RankMe service without third-party dependencies."""
import argparse
import fcntl
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch_json(url):
    with OPENER.open(url, timeout=1.5) as response:
        if response.status != 200:
            raise ValueError('Unexpected response')
        return json.loads(response.read(2_000_000))


def probe(port):
    """Identify RankMe, not merely a listening HTTP server."""
    base = 'http://127.0.0.1:%s' % port
    try:
        session = fetch_json(base + '/api/session')
        token = session.get('token')
        if not isinstance(token, str) or len(token) < 20:
            return None
        state = fetch_json(base + '/api/state')
        server = state.get('server', {})
        if server.get('local') is not True or not server.get('version') or not isinstance(state.get('clients'), list):
            return None
        return {'token_hash': hashlib.sha256(token.encode()).hexdigest(), 'state': state}
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def occupied(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def open_page(url, no_open):
    print('RankMe: ' + url)
    if not no_open:
        try:
            if not webbrowser.open(url):
                print('Open the URL above in your browser.')
        except Exception:
            print('Open the URL above in your browser.')


def stop_server(record_path, session):
    if not session:
        print('No RankMe service is responding on this port.')
        return 0
    if any(job.get('status') == 'running' for job in session['state'].get('jobs', [])):
        print('A job is running. Pause automation in Settings, wait for completion, then stop RankMe.')
        return 1
    try:
        record = json.loads(record_path.read_text(encoding='utf-8'))
        if record.get('token_hash') != session['token_hash']:
            raise ValueError('Session differs')
        pid = int(record['pid'])
        if pid <= 1:
            raise ValueError('Invalid process')
        command = subprocess.check_output(['/bin/ps', '-p', str(pid), '-o', 'command='], text=True).strip()
        if str(ROOT / 'run.py') not in command:
            raise ValueError('Process differs')
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print('This session was not started by this launcher. Stop it with Ctrl+C in its original terminal.')
        return 1
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        time.sleep(0.1)
        if not probe(record['port']):
            record_path.unlink(missing_ok=True)
            print('RankMe stopped. Local data is preserved.')
            return 0
    print('Shutdown requested. Wait a moment before restarting.')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Launch RankMe locally and open its dashboard')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--data-dir', default=str(ROOT / 'data'))
    parser.add_argument('--no-open', action='store_true', help='Start without opening a browser')
    parser.add_argument('--stop', action='store_true', help='Stop an idle service started by this launcher')
    parser.add_argument('--status', action='store_true', help='Show whether RankMe is running')
    args = parser.parse_args(argv)
    if sys.version_info < (3, 9):
        parser.error('Python 3.9 or later is required')
    if not 1024 <= args.port <= 65535:
        parser.error('Port must be between 1024 and 65535')
    data = Path(args.data_dir).expanduser().resolve()
    data.mkdir(parents=True, exist_ok=True)
    data.chmod(0o700)
    record_path = data / ('launcher-%s.json' % args.port)
    # Serialize launches from two Finder windows or terminals using the same data directory.
    with (data / 'launcher.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        session = probe(args.port)
        if args.stop:
            return stop_server(record_path, session)
        if args.status:
            print('RankMe is running.' if session else 'RankMe is not running on this port.')
            return 0 if session else 1
        if session:
            print('Using the existing RankMe session on this port.')
            open_page('http://127.0.0.1:%s' % args.port, args.no_open)
            return 0
        if occupied(args.port):
            print('Port %s is occupied by another service. Choose a different --port.' % args.port, file=sys.stderr)
            return 1
        env = dict(os.environ)
        extra = [str(Path.home() / '.local/bin'), '/opt/homebrew/bin', '/usr/local/bin']
        env['PATH'] = os.pathsep.join(extra + [env.get('PATH', '/usr/bin:/bin')])
        env['PYTHONUNBUFFERED'] = '1'
        log_path = data / 'server.log'
        with log_path.open('ab') as log:
            process = subprocess.Popen([sys.executable, str(ROOT / 'run.py'), '--port', str(args.port), '--data-dir', str(data)],
                                       cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       start_new_session=True, close_fds=True, env=env)
        for _ in range(80):
            if process.poll() is not None:
                print('RankMe did not start. Inspect ' + str(log_path), file=sys.stderr)
                return 1
            session = probe(args.port)
            if session:
                record = {'pid': process.pid, 'port': args.port, 'data_dir': str(data), 'token_hash': session['token_hash'],
                          'started_at': datetime.now(timezone.utc).isoformat()}
                record_path.write_text(json.dumps(record, indent=2), encoding='utf-8')
                record_path.chmod(0o600)
                open_page('http://127.0.0.1:%s' % args.port, args.no_open)
                print('Runs in the background while this computer is awake. Closing the browser does not stop it.')
                return 0
            time.sleep(0.15)
        process.terminate()
        print('RankMe startup timed out. Inspect ' + str(log_path), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
