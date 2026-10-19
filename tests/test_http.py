import unittest
from unittest.mock import patch

from gpta.http import FetchError, HttpClient
from gpta.module_api import FetchError as ModuleFetchError


class FakeResponse:
    def __init__(self, data: bytes):
        self.data = data

    def read(self, size=None):
        if size is None:
            return self.data
        return self.data[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class HttpClientTests(unittest.TestCase):
    def test_fetch_error_is_the_module_error(self):
        self.assertTrue(issubclass(FetchError, ModuleFetchError))

    def test_json_body_above_limit_raises(self):
        payload = b"[" + b"0," * 100 + b"0]"
        with patch(
            "gpta.http.urllib.request.urlopen", return_value=FakeResponse(payload)
        ):
            with self.assertRaises(FetchError) as caught:
                HttpClient().request_json(
                    "GET", "https://example.test/data", read_limit=50
                )
        self.assertIn("exceeds", str(caught.exception))

    def test_json_read_limit_can_be_overridden(self):
        payload = b"[1,2,3]"
        with patch(
            "gpta.http.urllib.request.urlopen", return_value=FakeResponse(payload)
        ):
            self.assertEqual(
                HttpClient().request_json(
                    "GET", "https://example.test/data", read_limit=100
                ),
                [1, 2, 3],
            )
            with self.assertRaises(FetchError):
                HttpClient().request_json(
                    "GET", "https://example.test/data", read_limit=3
                )

    def test_text_is_truncated_not_unbounded(self):
        payload = ("0123456789" * 10).encode()
        with patch(
            "gpta.http.urllib.request.urlopen", return_value=FakeResponse(payload)
        ):
            text = HttpClient().get_text("https://example.test/text", max_bytes=10)
        self.assertEqual(text, "0123456789")

    def test_default_json_cap_bounds_read(self):
        payload = b"x" * (2_000_001)
        with patch(
            "gpta.http.urllib.request.urlopen", return_value=FakeResponse(payload)
        ):
            with self.assertRaises(FetchError):
                HttpClient().get_json("https://example.test/huge")


if __name__ == "__main__":
    unittest.main()
