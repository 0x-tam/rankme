"""Local, single-owner WebAuthn authentication and private credential storage."""

import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from fido2.server import Fido2Server
from fido2.webauthn import (AttestedCredentialData, PublicKeyCredentialRpEntity,
                             ResidentKeyRequirement, UserVerificationRequirement)


CHALLENGE_TTL = 180
BOOTSTRAP_TTL = 600
SESSION_ABSOLUTE = 8 * 3600
SESSION_IDLE = 30 * 60
STEP_UP_TTL = 120
MAX_CREDENTIALS = 10


class AuthError(ValueError):
    """Expected authentication failure with a deliberately generic response."""

    status = 400


class RateLimited(AuthError):
    status = 429


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 8192 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise AuthError("Authentication failed")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError as exc:
        raise AuthError("Authentication failed") from exc


def _private_dir(data_dir):
    root = Path(data_dir).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder = root / "auth"
    if folder.is_symlink():
        raise RuntimeError("Authentication directory is unsafe")
    try:
        folder.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("Authentication directory must be owner-only")
    return folder


@contextmanager
def _disk_lock(data_dir):
    folder = _private_dir(data_dir)
    fd = os.open(folder / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError("Authentication lock is unsafe")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError("Authentication store unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 200_000:
            raise RuntimeError("Authentication store is unsafe")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            value = json.load(handle)
        return value
    except (OSError, ValueError, UnicodeError) as exc:
        raise RuntimeError("Authentication store is malformed") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _write(path, value):
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
        raise RuntimeError("Authentication store is unsafe")
    payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) > 200_000:
        raise RuntimeError("Authentication store is too large")
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".auth-", delete=False) as handle:
        temp = Path(handle.name)
        try:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            temp.unlink(missing_ok=True)
            raise
    os.replace(temp, path)


def _load(data_dir):
    folder = _private_dir(data_dir)
    path = folder / "credentials.json"
    marker = Path(data_dir).resolve() / ".auth-enrolled"
    record = _read(path)
    marked = _read(marker)
    if record is None:
        if marked is not None:
            raise RuntimeError("Authentication credentials are missing; local recovery is required")
        record = {"version": 1, "owner_id": _b64(secrets.token_bytes(32)), "credentials": [], "bootstrap": None}
        _write(path, record)
    if (not isinstance(record, dict) or record.get("version") != 1 or
            not isinstance(record.get("owner_id"), str) or len(_unb64(record["owner_id"])) != 32 or
            not isinstance(record.get("credentials"), list) or len(record["credentials"]) > MAX_CREDENTIALS or
            not all(isinstance(c, dict) and set(c) == {"id", "data", "name", "created_at", "counter"} and
                    isinstance(c["name"], str) and isinstance(c["created_at"], (int, float)) and
                    isinstance(c["counter"], int) and c["counter"] >= 0 and
                    isinstance(c["id"], str) and isinstance(c["data"], str) for c in record["credentials"])):
        raise RuntimeError("Authentication store is malformed")
    ids = set()
    try:
        for c in record["credentials"]:
            parsed = AttestedCredentialData(_unb64(c["data"]))
            if _b64(parsed.credential_id) != c["id"] or c["id"] in ids:
                raise ValueError()
            ids.add(c["id"])
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError("Authentication store is malformed") from exc
    if marked is not None and (marked != {"version": 1} or not record["credentials"]):
        raise RuntimeError("Authentication store is inconsistent")
    if record["credentials"] and marked is None:
        raise RuntimeError("Authentication enrollment marker is missing")
    bootstrap = record.get("bootstrap")
    if bootstrap is not None and (not isinstance(bootstrap, dict) or
            not isinstance(bootstrap.get("digest"), str) or len(bootstrap["digest"]) != 64 or
            not isinstance(bootstrap.get("expires"), (int, float))):
        raise RuntimeError("Authentication store is malformed")
    return path, marker, record


def issue_bootstrap(data_dir):
    """Issue a one-use first-owner invitation from a trusted local process only."""
    with _disk_lock(data_dir):
        path, _, record = _load(data_dir)
        if record["credentials"]:
            raise AuthError("Owner is already enrolled")
        secret = secrets.token_urlsafe(48)
        record["bootstrap"] = {"digest": _hash(secret), "expires": time.time() + BOOTSTRAP_TTL}
        _write(path, record)
        return secret


def reset_for_recovery(data_dir):
    """Discard passkeys only after the caller verifies local OS control and server stop."""
    with _disk_lock(data_dir):
        path, marker, _ = _load(data_dir)
        _write(path, {"version": 1, "owner_id": _b64(secrets.token_bytes(32)),
                      "credentials": [], "bootstrap": None})
        marker.unlink(missing_ok=True)


class AuthService:
    def __init__(self, data_dir, port=8787):
        self.data_dir = Path(data_dir).resolve()
        with _disk_lock(self.data_dir):
            self.path, self.marker, _ = _load(self.data_dir)
        self.port = port
        self.origin = f"http://localhost:{port}"
        self.server = Fido2Server(PublicKeyCredentialRpEntity(name="RankMe", id="localhost"),
                                  verify_origin=lambda origin: origin == self.origin)
        self.lock = threading.RLock()
        self.preauth = {}
        self.sessions = {}
        self.challenges = {}
        self.attempts = {}
        self.oauth = {}

    def _data(self):
        return _load(self.data_dir)[2]

    def enrolled(self):
        with self.lock:
            return bool(self._data()["credentials"])

    def _prune(self):
        current = time.time()
        for collection in (self.preauth, self.sessions, self.challenges, self.oauth):
            for key, value in list(collection.items()):
                expiry = value.get("expires", 0)
                if collection is self.sessions:
                    expiry = min(expiry, value.get("last", 0) + SESSION_IDLE)
                if expiry < current:
                    collection.pop(key, None)
        for key, value in list(self.attempts.items()):
            if value[0] < current - 60:
                self.attempts.pop(key, None)

    def _rate(self, key):
        self._prune()
        now = time.time()
        start, count = self.attempts.get(key, (now, 0))
        if now - start >= 60:
            start, count = now, 0
        if count >= 10:
            raise RateLimited("Too many attempts. Try again shortly")
        if len(self.attempts) >= 1024 and key not in self.attempts:
            raise RateLimited("Too many attempts. Try again shortly")
        self.attempts[key] = (start, count + 1)

    def preauth_status(self, raw=None):
        with self.lock:
            self._prune()
            key = _hash(raw) if raw and len(raw) <= 256 else ""
            item = self.preauth.get(key)
            if item is None:
                if len(self.preauth) >= 256:
                    raise RateLimited("Too many attempts. Try again shortly")
                raw = secrets.token_urlsafe(32)
                key = _hash(raw)
                item = {"csrf": secrets.token_urlsafe(32), "expires": time.time() + 600}
                self.preauth[key] = item
            return raw, item["csrf"]

    def preauth_check(self, raw, csrf):
        with self.lock:
            self._prune()
            key = _hash(raw) if raw and len(raw) <= 256 else ""
            entry = self.preauth.get(key)
            if not entry or not csrf or not secrets.compare_digest(entry["csrf"], csrf):
                raise AuthError("Authentication required")
            self._rate("pre:" + key)
            return key

    def session(self, raw, touch=True):
        with self.lock:
            self._prune()
            key = _hash(raw) if raw and len(raw) <= 256 else ""
            entry = self.sessions.get(key)
            if not entry:
                return None
            if touch:
                entry["last"] = time.time()
            return key, entry

    def new_session(self, credential_id):
        with self.lock:
            self._prune()
            if len(self.sessions) >= 128:
                oldest = min(self.sessions, key=lambda key: self.sessions[key]["last"])
                self.sessions.pop(oldest, None)
            raw = secrets.token_urlsafe(48)
            self.sessions[_hash(raw)] = {"csrf": secrets.token_urlsafe(32),
                                          "expires": time.time() + SESSION_ABSOLUTE,
                                          "last": time.time(), "step_up": 0,
                                          "credential_id": credential_id}
            return raw

    def logout(self, raw):
        with self.lock:
            self.sessions.pop(_hash(raw), None)

    def _begin(self, kind, binding, credentials=None, name=""):
        self._prune()
        if len(self.challenges) >= 256:
            raise RateLimited("Too many attempts. Try again shortly")
        key = (kind, binding)
        if kind in ("enroll", "add"):
            data = self._data()
            options, state = self.server.register_begin(
                {"id": _unb64(data["owner_id"]), "name": "owner", "displayName": "RankMe owner"},
                credentials=[AttestedCredentialData(_unb64(c["data"])) for c in data["credentials"]],
                resident_key_requirement=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED)
        else:
            options, state = self.server.authenticate_begin(
                credentials=[AttestedCredentialData(_unb64(c["data"])) for c in (credentials or [])],
                user_verification=UserVerificationRequirement.REQUIRED)
        self.challenges[key] = {"state": state, "expires": time.time() + CHALLENGE_TTL,
                                "name": str(name or "Passkey")[:80]}
        return dict(options)

    def _take(self, kind, binding):
        self._prune()
        challenge = self.challenges.pop((kind, binding), None)
        if not challenge:
            raise AuthError("Authentication request expired")
        return challenge

    @staticmethod
    def _client_response(credential):
        if not isinstance(credential, dict) or len(json.dumps(credential)) > 64000:
            raise AuthError("Authentication failed")
        response = credential.get("response")
        if not isinstance(response, dict):
            raise AuthError("Authentication failed")
        try:
            client = json.loads(_unb64(response["clientDataJSON"]))
            if not isinstance(client, dict) or client.get("crossOrigin") is True or "topOrigin" in client:
                raise AuthError("Authentication failed")
            if credential.get("type") != "public-key" or _unb64(credential.get("rawId")) != _unb64(credential.get("id")):
                raise AuthError("Authentication failed")
        except (KeyError, ValueError, TypeError, UnicodeError) as exc:
            raise AuthError("Authentication failed") from exc
        return credential

    def enroll_options(self, binding, bootstrap, name=""):
        with self.lock:
            self._rate("enroll:" + binding)
            data = self._data()
            token = data.get("bootstrap") or {}
            if data["credentials"] or not isinstance(bootstrap, str) or len(bootstrap) > 256 or not token or time.time() > token["expires"] or not secrets.compare_digest(_hash(bootstrap), token["digest"]):
                raise AuthError("Enrollment unavailable")
            options = self._begin("enroll", binding, name=name)
            self.challenges[("enroll", binding)]["bootstrap"] = token["digest"]
            return options

    def enroll_verify(self, binding, credential):
        with self.lock:
            challenge = self._take("enroll", binding)
            with _disk_lock(self.data_dir):
                data = self._data()
                token = data.get("bootstrap") or {}
                if data["credentials"] or not token or time.time() > token["expires"] or token["digest"] != challenge["bootstrap"]:
                    raise AuthError("Enrollment unavailable")
                auth_data = self._register(challenge, credential)
                self._save_credential(data, auth_data, challenge["name"])
                data["bootstrap"] = None
                _write(self.path, data)
                _write(self.marker, {"version": 1})
                return self.new_session(data["credentials"][0]["id"])

    def _register(self, challenge, credential):
        try:
            return self.server.register_complete(challenge["state"], self._client_response(credential))
        except (ValueError, KeyError, TypeError) as exc:
            raise AuthError("Authentication failed") from exc

    def _save_credential(self, data, auth_data, name):
        credential = auth_data.credential_data
        if credential is None or len(data["credentials"]) >= MAX_CREDENTIALS:
            raise AuthError("Passkey limit reached")
        ident = _b64(credential.credential_id)
        if any(c["id"] == ident for c in data["credentials"]):
            raise AuthError("Authentication failed")
        data["credentials"].append({"id": ident, "data": _b64(bytes(credential)),
                                     "name": name, "created_at": time.time(), "counter": auth_data.counter})

    def login_options(self, binding):
        with self.lock:
            self._rate("login:" + binding)
            data = self._data()
            if not data["credentials"]:
                raise AuthError("Authentication unavailable")
            return self._begin("login", binding, data["credentials"])

    def _authenticate(self, challenge, credential):
        try:
            parsed = self._client_response(credential)
            data = self._data()
            creds = [AttestedCredentialData(_unb64(c["data"])) for c in data["credentials"]]
            matched = self.server.authenticate_complete(challenge["state"], creds, parsed)
            from fido2.webauthn import AuthenticationResponse
            assertion = AuthenticationResponse.from_dict(parsed)
            counter = assertion.response.authenticator_data.counter
            item = next(c for c in data["credentials"] if c["id"] == _b64(matched.credential_id))
            if (item["counter"] or counter) and counter <= item["counter"]:
                raise AuthError("Authentication failed")
            handle = assertion.response.user_handle
            if handle is not None and handle != _unb64(data["owner_id"]):
                raise AuthError("Authentication failed")
            if counter:
                item["counter"] = counter
                _write(self.path, data)
            return item
        except (ValueError, KeyError, TypeError, StopIteration) as exc:
            raise AuthError("Authentication failed") from exc

    def login_verify(self, binding, credential):
        with self.lock:
            challenge = self._take("login", binding)
            item = self._authenticate(challenge, credential)
            return self.new_session(item["id"])

    def step_up_options(self, session_key):
        with self.lock:
            self._rate("step:" + session_key)
            return self._begin("step", session_key, self._data()["credentials"])

    def step_up_verify(self, session_key, credential):
        with self.lock:
            challenge = self._take("step", session_key)
            self._authenticate(challenge, credential)
            session = self.sessions.get(session_key)
            if not session:
                raise AuthError("Authentication required")
            session["step_up"] = time.time() + STEP_UP_TTL

    def _step(self, session_key, consume=False):
        session = self.sessions.get(session_key)
        if not session or session["step_up"] < time.time():
            raise AuthError("Fresh passkey verification required")
        if consume:
            session["step_up"] = 0

    def add_options(self, session_key, name=""):
        with self.lock:
            self._step(session_key)
            return self._begin("add", session_key, name=name)

    def add_verify(self, session_key, credential):
        with self.lock:
            self._step(session_key)
            challenge = self._take("add", session_key)
            auth_data = self._register(challenge, credential)
            data = self._data()
            self._save_credential(data, auth_data, challenge["name"])
            _write(self.path, data)
            self._step(session_key, consume=True)

    def list_credentials(self):
        with self.lock:
            return [{key: c[key] for key in ("id", "name", "created_at")} for c in self._data()["credentials"]]

    def remove(self, session_key, ident):
        with self.lock:
            self._step(session_key)
            data = self._data()
            if len(data["credentials"]) <= 1:
                raise AuthError("Keep at least one passkey")
            remaining = [c for c in data["credentials"] if c["id"] != ident]
            if len(remaining) == len(data["credentials"]):
                raise AuthError("Passkey not found")
            data["credentials"] = remaining
            _write(self.path, data)
            self._step(session_key, consume=True)
            for key, session in list(self.sessions.items()):
                if session["credential_id"] == ident:
                    self.sessions.pop(key, None)

    def begin_oauth(self, session_key, state):
        with self.lock:
            self._prune()
            if len(self.oauth) >= 128:
                raise RateLimited("Too many attempts. Try again shortly")
            self.oauth[_hash(state)] = {"session": session_key, "expires": time.time() + 600}

    def consume_oauth(self, session_key, state):
        with self.lock:
            self._prune()
            if not state or len(state) > 256:
                raise AuthError("Connection failed")
            state_key = _hash(state)
            pending = self.oauth.get(state_key)
            if not pending or not session_key or not secrets.compare_digest(pending["session"], session_key):
                raise AuthError("Connection failed")
            self.oauth.pop(state_key, None)
