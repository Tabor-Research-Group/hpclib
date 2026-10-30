"""HTTP enforcement for first-party expiring sessions, using loopback fixtures."""
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hpclib/servers"))
from token_auth import TokenRegistry, TokenAuth
import rest_server
from test_rest_server import RESTServerTestCase, TOKEN


class SessionHTTPTests(RESTServerTestCase):
    def setUp(self):
        super().setUp()
        self.path = str(self.tmp / "tokens.json")
        self.registry = TokenRegistry(self.path)
        self.server.auth = TokenAuth(TOKEN, tokens_file=self.path)
        self.server.audit = rest_server.AuditLog(str(self.tmp / "audit.jsonl"))
        self.grant = self.registry.create_grant("research", ["read", "files:write"], [str(self.root)])
        self.session = self.registry.issue(self.grant["id"])
        self.agent = self.session["token"]

    def test_health_and_stable_identity(self):
        status, body = self.request("GET", "/health", token=self.agent)
        self.assertEqual(status, 200)
        self.assertEqual(body["auth_api"], 2)
        self.assertEqual(body["token"]["name"], "research")
        self.assertEqual(self.request("GET", "/auth/session", token=self.agent)[1]["session_id"], self.session["session"]["id"])

    def test_admin_only_minting(self):
        for route in ("/admin/auth/grants", "/admin/auth/sessions", "/admin/auth/revoke"):
            self.assertEqual(self.request("POST", route, token=self.agent, json_body={})[0], 403)
        status, grant = self.request("POST", "/admin/auth/grants", json_body={"name": "other", "scopes": ["read"], "allow": [str(self.root)]})
        self.assertEqual(status, 200, grant)
        status, session = self.request("POST", "/admin/auth/sessions", json_body={"grant_id": grant["id"]})
        self.assertEqual(status, 200, session)
        self.assertEqual(self.request("GET", "/health", token=session["token"])[1]["token"]["name"], "other")

    def test_rotation_and_credential_redaction(self):
        status, fresh = self.request("POST", "/auth/session/rotate", token=self.agent, json_body={"ttl": 120})
        self.assertEqual(status, 200, fresh)
        self.assertEqual(self.request("GET", "/health", token=self.agent)[0], 401)
        self.assertEqual(self.request("GET", "/health", token=fresh["token"])[0], 200)
        status, listed = self.request("GET", "/admin/auth")
        self.assertEqual(status, 200)
        self.assertNotIn("sha256", json.dumps(listed))
        audit = (self.tmp / "audit.jsonl").read_text()
        self.assertNotIn(self.agent, audit)
        self.assertNotIn(fresh["token"], audit)
        self.assertIn(self.session["session"]["id"], audit)

    def test_scope_and_path_checks_survive_delegation(self):
        status, child = self.request("POST", "/auth/session/delegate", token=self.agent,
                                     json_body={"scopes": ["read"], "allow": [str(self.root / "sub")]})
        self.assertEqual(status, 200, child)
        self.assertEqual(self.request("PUT", "/files/content", token=child["token"], query={"path": "x"}, data=b"x")[0], 403)
        self.assertEqual(self.request("GET", "/files/content", token=child["token"], query={"path": str(self.outside / "secret.txt")})[0], 403)
        self.assertEqual(self.request("POST", "/slurm/sbatch", token=child["token"], json_body={})[0], 403)

    def test_delegate_cannot_widen_and_legacy_cannot_delegate(self):
        self.assertEqual(self.request("POST", "/auth/session/delegate", token=self.agent, json_body={"scopes": ["submit"]})[0], 403)
        legacy = TokenAuth.add_token(self.path, "old", ["read"], [str(self.root)])
        for token in (legacy, TOKEN):
            self.assertEqual(self.request("POST", "/auth/session/delegate", token=token, json_body={})[0], 403)
            self.assertEqual(self.request("POST", "/auth/session/rotate", token=token, json_body={})[0], 403)

    def test_logout_revokes_descendants(self):
        _, child = self.request("POST", "/auth/session/delegate", token=self.agent, json_body={})
        self.assertEqual(self.request("DELETE", "/auth/session", token=self.agent)[0], 200)
        self.assertEqual(self.request("GET", "/health", token=child["token"])[0], 401)
        self.assertEqual(self.request("POST", "/auth/session/rotate", token=self.agent, json_body={})[0], 401)

    def test_update_and_revoke_grant_are_enforced(self):
        status, body = self.request("PUT", "/admin/auth/grants", json_body={"grant_id": self.grant["id"], "scopes": ["read"], "allow": [str(self.root)]})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.request("PUT", "/files/content", token=self.agent, query={"path": "x"}, data=b"x")[0], 403)
        self.assertEqual(self.request("POST", "/admin/auth/revoke", json_body={"record_id": self.grant["id"]})[0], 200)
        self.assertEqual(self.request("GET", "/health", token=self.agent)[0], 401)

    def test_bad_fields_and_owner_minting_cannot_be_smuggled(self):
        for body in ({"ttl": "forever"}, {"token": TOKEN}, {"scopes": ["*"]}, {"unexpected": True}):
            status, out = self.request("POST", "/auth/session/delegate", token=self.agent, json_body=body)
            self.assertEqual(status, 400, out)
