import os
import tempfile
import unittest

from liftosaur_sync import build_strava_authorize_url, extract_strava_code, persist_env_value


class StravaAuthTests(unittest.TestCase):
    def test_builds_authorize_url_with_required_scope(self):
        url = build_strava_authorize_url("123", "http://localhost/exchange_token")

        self.assertIn("client_id=123", url)
        self.assertIn("response_type=code", url)
        self.assertIn("activity%3Aread_all%2Cactivity%3Awrite", url)

    def test_extracts_code_from_redirect_url(self):
        code = extract_strava_code("http://localhost/exchange_token?state=&code=abc123&scope=read,activity:write")

        self.assertEqual(code, "abc123")

    def test_accepts_raw_code(self):
        self.assertEqual(extract_strava_code("abc123"), "abc123")

    def test_persist_env_value_replaces_existing_key_and_appends_new_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, ".env")
            with open(path, "w", encoding="utf-8") as file:
                file.write("STRAVA_CLIENT_ID=1\nSTRAVA_REFRESH_TOKEN=old\n")

            persist_env_value(path, "STRAVA_REFRESH_TOKEN", "new")
            persist_env_value(path, "STRAVA_REDIRECT_URI", "http://localhost/exchange_token")

            with open(path, encoding="utf-8") as file:
                contents = file.read()
        self.assertIn("STRAVA_CLIENT_ID=1\n", contents)
        self.assertIn("STRAVA_REFRESH_TOKEN=new\n", contents)
        self.assertNotIn("STRAVA_REFRESH_TOKEN=old", contents)
        self.assertIn("STRAVA_REDIRECT_URI=http://localhost/exchange_token\n", contents)


if __name__ == "__main__":
    unittest.main()
