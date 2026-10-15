import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from app.api.endpoints import authenticate_request, require_workbook_owner
from app.security import ApiKeyAuthenticator


ALICE_KEY = "alice-api-key-with-at-least-32-characters"
BOB_KEY = "bob-api-key-with-at-least-32-characters--"


class _FakeService:
    def assert_workbook_owner(self, workbook_id, principal):
        if (workbook_id, principal) != ("alice-book", "alice"):
            raise ValueError("not found")

    def assert_import_owner(self, import_id, principal):
        if (import_id, principal) != ("alice-import", "alice"):
            raise ValueError("not found")

    def execute_readonly_sql(self, workbook_id, sql, max_rows):
        return {
            "sql": sql,
            "columns": ["answer"],
            "rows": [[42]],
            "row_count": 1,
            "truncated": False,
            "elapsed_ms": 0.1,
        }


class ApiAuthTests(unittest.TestCase):
    def setUp(self):
        self.service = _FakeService()
        self.authenticator = ApiKeyAuthenticator.from_config(
            {},
            {
                "RONGNENG_API_KEYS_JSON": (
                    '{"alice":"' + ALICE_KEY + '","bob":"' + BOB_KEY + '"}'
                )
            },
        )
        self.request = SimpleNamespace(
            url=SimpleNamespace(path="/api/v1/workbooks/alice-book/query"),
            app=SimpleNamespace(
                state=SimpleNamespace(authenticator=self.authenticator)
            ),
            state=SimpleNamespace(),
        )

    def test_health_is_public_but_data_routes_require_authentication(self):
        health_request = SimpleNamespace(
            url=SimpleNamespace(path="/api/v1/health"),
            app=self.request.app,
            state=SimpleNamespace(),
        )
        self.assertEqual("health", authenticate_request(health_request, None, None))
        with self.assertRaises(HTTPException) as raised:
            authenticate_request(self.request, None, None)
        self.assertEqual(401, raised.exception.status_code)
        self.assertEqual("Bearer", raised.exception.headers["WWW-Authenticate"])

    def test_authenticated_principal_cannot_cross_workbook_owner_boundary(self):
        bob = authenticate_request(self.request, None, BOB_KEY)
        with self.assertRaises(HTTPException) as raised:
            require_workbook_owner("alice-book", bob, self.service)
        self.assertEqual(404, raised.exception.status_code)

        alice = authenticate_request(
            self.request, f"Bearer {ALICE_KEY}", None
        )
        self.assertEqual(
            "alice",
            require_workbook_owner("alice-book", alice, self.service),
        )


if __name__ == "__main__":
    unittest.main()
