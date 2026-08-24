"""Access control.

The API resolves operator-supplied URLs server-side and writes files to disk, so
a reachable unauthenticated instance is both an SSRF and a disk-write primitive.
Loopback alone is not enough: DNS rebinding lets a page reach 127.0.0.1 as
same-origin, which is why the Host header is checked too.
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from videotrack.server.app import create_app
from videotrack.server.security import (
    InsecureConfiguration,
    host_is_allowed,
    verify_configuration,
)
from videotrack.server.settings import Settings


def _client(settings: Settings, token: str = "") -> TestClient:
    # TestClient sends Host: testserver by default, which the DNS-rebinding
    # guard correctly rejects. A real browser sends the real host, so the
    # base URL is set to match.
    return TestClient(
        create_app(settings, db_path=":memory:", token=token),
        base_url=f"http://{settings.host}:{settings.port}",
    )


class ConfigurationTests(unittest.TestCase):
    def test_a_non_loopback_bind_without_a_token_is_refused(self) -> None:
        with self.assertRaises(InsecureConfiguration):
            verify_configuration(Settings(host="0.0.0.0"), "")

    def test_a_non_loopback_bind_with_a_token_is_allowed(self) -> None:
        verify_configuration(Settings(host="0.0.0.0"), "secret")

    def test_a_loopback_bind_needs_no_token(self) -> None:
        verify_configuration(Settings(host="127.0.0.1"), "")

    def test_creating_the_app_refuses_an_unprotected_wide_bind(self) -> None:
        with self.assertRaises(InsecureConfiguration):
            create_app(Settings(host="0.0.0.0"), db_path=":memory:", token="")


class HostGuardTests(unittest.TestCase):
    def test_loopback_names_are_allowed(self) -> None:
        settings = Settings(host="127.0.0.1")

        for host in ("127.0.0.1", "127.0.0.1:8756", "localhost", "localhost:8756", "[::1]:8756"):
            with self.subTest(host=host):
                self.assertTrue(host_is_allowed(host, settings))

    def test_a_rebinding_hostname_is_rejected(self) -> None:
        settings = Settings(host="127.0.0.1")

        self.assertFalse(host_is_allowed("evil.example.test", settings))
        self.assertFalse(host_is_allowed("evil.example.test:8756", settings))

    def test_a_missing_host_header_is_rejected(self) -> None:
        self.assertFalse(host_is_allowed("", Settings(host="127.0.0.1")))

    def test_a_deliberate_wide_bind_accepts_any_name(self) -> None:
        # Such a bind is protected by the token, not by the hostname.
        self.assertTrue(host_is_allowed("downloader.lan", Settings(host="0.0.0.0")))


class RequestGuardTests(unittest.TestCase):
    def test_a_loopback_request_needs_no_token(self) -> None:
        with _client(Settings(host="127.0.0.1")) as client:
            self.assertEqual(client.get("/api/settings").status_code, 200)

    def test_a_rebinding_host_header_is_rejected(self) -> None:
        with _client(Settings(host="127.0.0.1")) as client:
            response = client.get("/api/settings", headers={"host": "evil.example.test"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["reason"], "host_not_allowed")

    def test_a_configured_token_is_required(self) -> None:
        with _client(Settings(host="127.0.0.1"), token="secret") as client:
            response = client.get("/api/settings")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"]["reason"], "token_required")

    def test_a_wrong_token_is_rejected(self) -> None:
        with _client(Settings(host="127.0.0.1"), token="secret") as client:
            response = client.get("/api/settings", headers={"authorization": "Bearer nope"})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"]["reason"], "token_invalid")

    def test_the_right_token_is_accepted(self) -> None:
        with _client(Settings(host="127.0.0.1"), token="secret") as client:
            response = client.get("/api/settings", headers={"authorization": "Bearer secret"})

        self.assertEqual(response.status_code, 200)

    def test_the_bearer_prefix_is_case_insensitive(self) -> None:
        with _client(Settings(host="127.0.0.1"), token="secret") as client:
            response = client.get("/api/settings", headers={"authorization": "bearer secret"})

        self.assertEqual(response.status_code, 200)


class FrontendMountTests(unittest.TestCase):
    def test_the_api_answers_before_the_frontend_is_built(self) -> None:
        # A clean checkout must be able to start and report tool availability.
        with _client(Settings(host="127.0.0.1")) as client:
            health = client.get("/api/health")

        self.assertEqual(health.status_code, 200)

    def test_the_default_test_host_is_still_rejected(self) -> None:
        # Proof the guard is live: the harness default is not a loopback name.
        client = TestClient(create_app(Settings(host="127.0.0.1"), db_path=":memory:"))
        with client:
            self.assertEqual(client.get("/api/settings").status_code, 400)


if __name__ == "__main__":
    unittest.main()
