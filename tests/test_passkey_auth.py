"""Cryptographic and state-bound passkey regression tests."""

import base64
import hashlib
import io
import json
import secrets
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fido2 import cose, webauthn

from rankme.auth import AuthError, AuthService, issue_bootstrap
from rankme.server import Application, handler_for


def b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class PasskeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = AuthService(self.root)
        self.secret = issue_bootstrap(self.root)
        self.browser, csrf = self.auth.preauth_status()
        self.binding = self.auth.preauth_check(self.browser, csrf)
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)

    def tearDown(self):
        self.temp.cleanup()

    def registration(self, options, *, origin="http://localhost:8787", rp="localhost", uv=True,
                     challenge=None, credential_id=None):
        ident = credential_id or self.credential_id
        credential = webauthn.AttestedCredentialData.create(
            bytes(16), ident, cose.ES256.from_cryptography_key(self.key.public_key()))
        flags = webauthn.AuthenticatorData.FLAG.UP | webauthn.AuthenticatorData.FLAG.AT
        if uv:
            flags |= webauthn.AuthenticatorData.FLAG.UV
        auth_data = webauthn.AuthenticatorData.create(hashlib.sha256(rp.encode()).digest(), flags, 0, credential)
        attestation = webauthn.AttestationObject.create("none", auth_data, {})
        client = json.dumps({"type": "webauthn.create", "challenge": challenge or options["publicKey"]["challenge"],
                             "origin": origin}).encode()
        return {"id": b64(ident), "rawId": b64(ident), "type": "public-key",
                "response": {"clientDataJSON": b64(client), "attestationObject": b64(attestation)}}

    def assertion(self, options, *, origin="http://localhost:8787", rp="localhost", uv=True,
                  counter=0, key=None, ident=None, challenge=None, user_handle=None, cross_origin=False):
        ident = ident or self.credential_id
        flags = webauthn.AuthenticatorData.FLAG.UP
        if uv:
            flags |= webauthn.AuthenticatorData.FLAG.UV
        auth_data = webauthn.AuthenticatorData.create(hashlib.sha256(rp.encode()).digest(), flags, counter)
        client_dict = {"type": "webauthn.get", "challenge": challenge or options["publicKey"]["challenge"],
                       "origin": origin}
        if cross_origin:
            client_dict["crossOrigin"] = True
        client = json.dumps(client_dict).encode()
        signature = (key or self.key).sign(bytes(auth_data) + hashlib.sha256(client).digest(),
                                           ec.ECDSA(hashes.SHA256()))
        response = {"clientDataJSON": b64(client), "authenticatorData": b64(auth_data),
                    "signature": b64(signature)}
        if user_handle is not None:
            response["userHandle"] = b64(user_handle)
        return {"id": b64(ident), "rawId": b64(ident), "type": "public-key", "response": response}

    def enroll(self):
        options = self.auth.enroll_options(self.binding, self.secret)
        return self.auth.enroll_verify(self.binding, self.registration(options))

    def login_options(self):
        browser, csrf = self.auth.preauth_status()
        binding = self.auth.preauth_check(browser, csrf)
        return binding, self.auth.login_options(binding)

    def test_bootstrap_one_use_private_and_restart_invalidates_session(self):
        raw_session = self.enroll()
        self.assertIsNotNone(self.auth.session(raw_session))
        self.assertFalse((self.root / "auth" / "credentials.json").stat().st_mode & 0o077)
        self.assertFalse((self.root / "auth").stat().st_mode & 0o077)
        self.assertNotIn(self.secret, (self.root / "auth" / "credentials.json").read_text())
        with self.assertRaises(AuthError):
            issue_bootstrap(self.root)
        self.assertIsNone(AuthService(self.root).session(raw_session))

    def test_bootstrap_reissue_invalidates_old_code_and_challenge_expires(self):
        options = self.auth.enroll_options(self.binding, self.secret)
        issue_bootstrap(self.root)
        with self.assertRaises(AuthError):
            self.auth.enroll_verify(self.binding, self.registration(options))
        current = issue_bootstrap(self.root)
        options = self.auth.enroll_options(self.binding, current)
        self.auth.challenges[("enroll", self.binding)]["expires"] -= 181
        with self.assertRaises(AuthError):
            self.auth.enroll_verify(self.binding, self.registration(options))
        self.assertFalse(self.auth.enrolled())

    def test_registration_options_include_browser_required_display_name(self):
        options = self.auth.enroll_options(self.binding, self.secret)
        self.assertEqual(options["publicKey"]["user"]["displayName"], "RankMe owner")

    def test_registration_rejects_wrong_origin_rp_uv_and_challenge(self):
        for change in ({"origin": "http://127.0.0.1:8787"}, {"rp": "127.0.0.1"},
                       {"uv": False}, {"challenge": b64(secrets.token_bytes(32))}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                auth = AuthService(directory)
                secret = issue_bootstrap(directory)
                browser, csrf = auth.preauth_status()
                binding = auth.preauth_check(browser, csrf)
                options = auth.enroll_options(binding, secret)
                with self.assertRaises(AuthError):
                    auth.enroll_verify(binding, self.registration(options, **change))
                self.assertFalse(auth.enrolled())

    def test_login_rejects_replay_cookie_mismatch_signature_rp_uv_and_user_handle(self):
        self.enroll()
        for change in ({"origin": "http://localhost:8788"}, {"rp": "evil.test"},
                       {"uv": False}, {"key": ec.generate_private_key(ec.SECP256R1())},
                       {"ident": secrets.token_bytes(32)}, {"user_handle": secrets.token_bytes(32)},
                       {"cross_origin": True}):
            with self.subTest(change=change):
                binding, options = self.login_options()
                with self.assertRaises(AuthError):
                    self.auth.login_verify(binding, self.assertion(options, **change))
                with self.assertRaises(AuthError):
                    self.auth.login_verify(binding, self.assertion(options))
        binding, options = self.login_options()
        other, csrf = self.auth.preauth_status()
        other_binding = self.auth.preauth_check(other, csrf)
        with self.assertRaises(AuthError):
            self.auth.login_verify(other_binding, self.assertion(options))
        self.assertIsNotNone(self.auth.login_verify(binding, self.assertion(options)))

    def test_counter_regression_rejected_and_zero_zero_allowed(self):
        self.enroll()
        binding, options = self.login_options()
        self.auth.login_verify(binding, self.assertion(options, counter=3))
        for count in (3, 2, 0):
            binding, options = self.login_options()
            with self.assertRaises(AuthError):
                self.auth.login_verify(binding, self.assertion(options, counter=count))

    def test_step_up_required_for_add_remove_and_last_passkey_kept(self):
        raw = self.enroll()
        session_key = self.auth.session(raw)[0]
        with self.assertRaises(AuthError):
            self.auth.add_options(session_key)
        with self.assertRaises(AuthError):
            self.auth.remove(session_key, b64(self.credential_id))
        options = self.auth.step_up_options(session_key)
        self.auth.step_up_verify(session_key, self.assertion(options))
        options = self.auth.add_options(session_key, "Second")
        second = secrets.token_bytes(32)
        self.auth.add_verify(session_key, self.registration(options, credential_id=second))
        self.assertEqual(len(self.auth.list_credentials()), 2)
        with self.assertRaises(AuthError):
            self.auth.remove(session_key, b64(second))
        options = self.auth.step_up_options(session_key)
        self.auth.step_up_verify(session_key, self.assertion(options))
        self.auth.remove(session_key, b64(second))
        self.assertEqual(len(self.auth.list_credentials()), 1)
        options = self.auth.step_up_options(session_key)
        self.auth.step_up_verify(session_key, self.assertion(options))
        with self.assertRaises(AuthError):
            self.auth.remove(session_key, b64(self.credential_id))

    def test_missing_or_malformed_store_fails_closed_after_enrollment(self):
        self.enroll()
        path = self.root / "auth" / "credentials.json"
        path.unlink()
        with self.assertRaises(RuntimeError):
            AuthService(self.root)
        path.write_text("not-json")
        with self.assertRaises(RuntimeError):
            AuthService(self.root)

    def test_passive_reads_do_not_extend_idle_session(self):
        raw = self.enroll()
        key, entry = self.auth.session(raw)
        entry["last"] -= 1799
        previous = entry["last"]
        self.auth.session(raw, touch=False)
        self.assertEqual(previous, entry["last"])
        self.auth.session(raw, touch=True)
        self.assertGreater(entry["last"], previous)
        entry["last"] -= 1801
        self.assertIsNone(self.auth.session(raw, touch=False))


class HTTPGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(self.temp.name)

    def tearDown(self):
        self.app.store.close()
        self.temp.cleanup()

    def request(self, method, path, headers=None, data=None):
        handler = object.__new__(handler_for(self.app))
        handler.server = SimpleNamespace(server_port=8787)
        handler.path = path
        body = json.dumps(data or {}).encode()
        handler.headers = {"Host": "localhost:8787", "Content-Length": str(len(body)), **(headers or {})}
        handler.rfile = io.BytesIO(body)
        handler.send = Mock()
        handler.handle_request(method)
        return handler.send.call_args.args

    def test_private_routes_and_downloads_require_session(self):
        for path in ("/api/session", "/api/state", "/api/backup", "/api/google/properties",
                     "/api/articles/abc/cover", "/api/articles/abc/download", "/api/auth/credentials"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 401)

    def test_mutations_require_exact_origin_session_and_csrf(self):
        raw = self.app.auth.new_session("fixture")
        csrf = self.app.auth.session(raw)[1]["csrf"]
        cookie = "__Host-rankme-session=" + raw
        for headers in ({"Cookie": cookie, "X-RankMe-CSRF": csrf},
                        {"Cookie": cookie, "X-RankMe-CSRF": csrf, "Origin": "http://127.0.0.1:8787"},
                        {"Cookie": cookie, "Origin": "http://localhost:8787"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/api/auth/logout", headers)[0], 403)
        headers = {"Cookie": cookie, "Origin": "http://localhost:8787", "X-RankMe-CSRF": csrf,
                   "Content-Type": "application/json"}
        self.assertEqual(self.request("POST", "/api/auth/logout", headers)[0], 200)
        self.assertEqual(self.request("GET", "/api/session", {"Cookie": cookie})[0], 401)


if __name__ == "__main__":
    unittest.main()
