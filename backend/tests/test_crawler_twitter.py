import json
import unittest
from unittest.mock import MagicMock

import requests

from app.crawler_manager import CrawlerTaskManager


def response(url, body, content_type="text/html; charset=utf-8"):
    item = requests.Response()
    item.status_code = 200
    item.url = url
    item._content = body.encode("utf-8") if isinstance(body, str) else body
    item.encoding = "utf-8"
    item.headers["content-type"] = content_type
    return item


class TwitterPublicCollectionTests(unittest.TestCase):
    def setUp(self):
        self.manager = object.__new__(CrawlerTaskManager)
        self.manager._checkpoint = MagicMock()
        self.manager._update = MagicMock()

    def test_account_timeline_and_oembed_need_no_bearer_token(self):
        profile = response(
            "https://syndication.twitter.com/srv/timeline-profile/screen-name/OpenAI",
            '<a href="https://x.com/OpenAI/status/1234567890123456789">one</a>'
            '<a href="/OpenAI/status/2234567890123456789">two</a>',
        )

        def request(url, *_args, **_kwargs):
            if "timeline-profile" in url:
                return profile
            status_id = "2234567890123456789" if "2234567890123456789" in url else "1234567890123456789"
            payload = {
                "url": f"https://x.com/OpenAI/status/{status_id}",
                "author_name": "OpenAI",
                "html": (
                    '<blockquote><p>Public post content</p>'
                    f'<a href="https://x.com/OpenAI/status/{status_id}">September 8, 2026</a></blockquote>'
                ),
            }
            return response(url, json.dumps(payload), "application/json")

        self.manager._request = MagicMock(side_effect=request)
        result = self.manager._discover_twitter_posts(
            "@OpenAI", {"max_items": 2, "proxies": []}, "test"
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["prefill"]["author"], "OpenAI")
        self.assertEqual(result[0]["prefill"]["text"], "Public post content")
        self.assertTrue(all("status/" in item["url"] for item in result))

    def test_capabilities_do_not_require_x_bearer_token(self):
        capabilities = self.manager.capabilities()
        self.assertTrue(capabilities["twitter_api_configured"])
        self.assertFalse(capabilities["twitter_token_required"])
        self.assertEqual(capabilities["twitter_mode"], "public_index_oembed")

    def test_user_mode_extracts_public_profile_fields(self):
        profile_data = {"props": {"pageProps": {"user": {
            "screen_name": "OpenAI", "name": "OpenAI", "description": "AI research",
            "location": "San Francisco", "followers_count": 123, "friends_count": 4,
            "statuses_count": 56, "created_at": "Sat Dec 03 20:40:00 +0000 2015",
            "verified": True, "profile_image_url_https": "https://img.example/avatar.jpg",
        }}}}
        html = '<html><head><meta property="og:title" content="OpenAI (@OpenAI)"></head>' \
               f'<body><script id="__NEXT_DATA__" type="application/json">{json.dumps(profile_data)}</script></body></html>'
        self.manager._request = MagicMock(return_value=response(
            "https://syndication.twitter.com/srv/timeline-profile/screen-name/OpenAI", html
        ))
        result = self.manager._discover_twitter_posts(
            "@OpenAI", {"twitter_mode": "user", "max_items": 1, "proxies": []}, "test"
        )
        row = result[0]["prefill"]
        self.assertEqual(row["username"], "OpenAI")
        self.assertEqual(row["followers"], 123)
        self.assertEqual(row["bio"], "AI research")
        self.assertTrue(row["verified"])

    def test_user_mode_has_its_own_field_set(self):
        names = [item["name"] for item in self.manager.builtin_fields("twitter", "user")]
        self.assertIn("username", names)
        self.assertIn("followers", names)
        self.assertNotIn("text", names)


if __name__ == "__main__":
    unittest.main()
