from __future__ import annotations

import json
import unittest

from mini_harness.secret_redaction import REDACTED, redact_sensitive_text


class SecretRedactionTests(unittest.TestCase):
    def test_redacts_self_identifying_credentials_without_labels(self) -> None:
        samples = (
            "sk-proj-UNLABELED_SECRET_ABC123",
            "github_pat_ABCDEF1234567890",
            # Assemble provider-shaped fixtures at runtime so repository
            # secret scanners do not mistake inert test data for credentials.
            "xoxb-" + "1234567890-abcdefghijklmnop",
            "AIza" + "A" * 35,
            "AKIA" + "A1" * 8,
            "sk_live_abcdefghijklmnop",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.signature1234",
        )

        for secret in samples:
            with self.subTest(secret=secret[:12]):
                result = redact_sensitive_text(f"before {secret} after")
                self.assertNotIn(secret, result)
                self.assertEqual(result, f"before {REDACTED} after")

    def test_redacts_labeled_structures_and_url_credentials(self) -> None:
        source = (
            '{"client_secret": "opaque-json"}\n'
            "SERVICE_REFRESH_TOKEN=opaque-env\n"
            "Authorization: Basic dXNlcjpwYXNz\n"
            "https://user:plainpass@example.com/callback"
            "?access_token=opaque-query&state=visible"
        )

        result = redact_sensitive_text(source)

        self.assertNotIn("opaque-json", result)
        self.assertNotIn("opaque-env", result)
        self.assertNotIn("dXNlcjpwYXNz", result)
        self.assertNotIn("plainpass", result)
        self.assertNotIn("opaque-query", result)
        self.assertIn("state=visible", result)
        parsed = json.loads(result.splitlines()[0])
        self.assertEqual(parsed["client_secret"], REDACTED)

    def test_redacts_complete_private_key_block(self) -> None:
        private_key = (
            "-----BEGIN PRIVATE KEY-----\n"
            "ABCDEF1234567890\n"
            "-----END PRIVATE KEY-----"
        )

        result = redact_sensitive_text(f"key:\n{private_key}\ndone")

        self.assertNotIn("ABCDEF1234567890", result)
        self.assertEqual(result, f"key:\n{REDACTED}\ndone")

    def test_does_not_redact_short_examples_or_normal_identifiers(self) -> None:
        source = (
            "Use sk-example in documentation; token_count=42; "
            "session_id=abc; state=visible; github_pat_demo"
        )

        self.assertEqual(redact_sensitive_text(source), source)


if __name__ == "__main__":
    unittest.main()
