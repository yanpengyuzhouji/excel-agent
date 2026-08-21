import unittest

from app.security import (
    ApiKeyAuthenticator,
    AuthConfigurationError,
    AuthenticationError,
)


class ApiKeyAuthenticatorTests(unittest.TestCase):
    def test_missing_configuration_fails_closed(self):
        authenticator = ApiKeyAuthenticator.from_config({}, {})
        with self.assertRaises(AuthConfigurationError):
            authenticator.authenticate()

    def test_same_key_protocol_supports_bearer_and_x_api_key(self):
        key = "excel-secure-api-key-with-32-characters"
        authenticator = ApiKeyAuthenticator.from_config(
            {}, {"RONGNENG_API_KEYS_JSON": '{"alice":"' + key + '"}'}
        )
        self.assertEqual("alice", authenticator.authenticate(f"Bearer {key}"))
        self.assertEqual("alice", authenticator.authenticate(x_api_key=key))
        with self.assertRaises(AuthenticationError):
            authenticator.authenticate(x_api_key="not-valid")


if __name__ == "__main__":
    unittest.main()
