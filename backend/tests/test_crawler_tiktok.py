import json
import unittest
from unittest.mock import Mock

from app.crawler_manager import CrawlerTaskManager
from app.douyin_profiles import _douyin_host, profile_link_rows, profile_rows


class TikTokCrawlerTests(unittest.TestCase):
    def setUp(self):
        self.manager = object.__new__(CrawlerTaskManager)
        self.manager._checkpoint = Mock()

    def test_mode_specific_builtin_fields(self):
        user = [item["name"] for item in self.manager.builtin_fields("tiktok", tiktok_mode="user")]
        comments = [item["name"] for item in self.manager.builtin_fields("tiktok", tiktok_mode="comments")]
        self.assertIn("followers", user)
        self.assertIn("avatar", user)
        self.assertEqual(comments, ["author", "username", "text", "published_at", "likes", "replies", "url"])

    def test_username_and_video_url_normalization(self):
        self.assertEqual(self.manager._tiktok_username("https://www.tiktok.com/@openai"), "openai")
        self.assertEqual(self.manager._tiktok_username("@open.ai"), "open.ai")
        self.assertEqual(
            self.manager._tiktok_video_url("https://www.tiktok.com/@openai/video/123456?lang=en"),
            "https://www.tiktok.com/@openai/video/123456",
        )

    def test_profile_state_is_mapped(self):
        state = {"scope": {"userInfo": {"user": {
            "uniqueId": "openai", "nickname": "OpenAI", "signature": "Hello",
            "verified": True, "avatarLarger": "https://img/avatar.jpg",
        }, "stats": {"followerCount": 12, "followingCount": 3, "videoCount": 4, "heartCount": 55}}}}
        html = '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">' + json.dumps(state) + "</script>"
        row = self.manager._tiktok_user_from_html("openai", html)
        self.assertEqual(row["followers"], 12)
        self.assertEqual(row["videos"], 4)
        self.assertTrue(row["verified"])

    def test_video_metadata_is_mapped(self):
        row = self.manager._tiktok_video_row({
            "id": "123", "uploader_id": "openai", "uploader": "OpenAI",
            "description": "demo", "view_count": 9, "like_count": 2, "comment_count": 1,
        })
        self.assertEqual(row["username"], "openai")
        self.assertEqual(row["url"], "https://www.tiktok.com/@openai/video/123")
        self.assertEqual(row["comments"], 1)

    def test_download_row_keeps_discovery_fields_missing_from_downloader(self):
        row = {key: "" for key in (
            "title", "author", "username", "comments", "shares", "thumbnail", "url"
        )}
        result = self.manager._fill_tiktok_download_row(
            row,
            {"title": "Downloaded title", "webpage_url": "https://www.tiktok.com/@demo/video/123"},
            {"author": "Demo", "username": "demo", "comments": 12,
             "shares": 3, "thumbnail": "https://img.example/cover.jpg"},
        )
        self.assertEqual(result["title"], "Downloaded title")
        self.assertEqual(result["author"], "Demo")
        self.assertEqual(result["username"], "demo")
        self.assertEqual(result["comments"], 12)
        self.assertEqual(result["shares"], 3)
        self.assertEqual(result["thumbnail"], "https://img.example/cover.jpg")

    def test_structured_sources_keep_all_selected_columns(self):
        row = self.manager._selected_prefill_row(
            [{"name": "author"}, {"name": "comments"}, {"name": "url"}],
            {"author": "Demo", "url": "https://example.test/item", "extra": "ignored"},
        )
        self.assertEqual(row, {
            "author": "Demo", "comments": "", "url": "https://example.test/item"
        })

    def test_comment_rows_are_deduplicated(self):
        rows = self.manager._tiktok_comment_rows([
            {"id": "1", "username": "a", "text": " Great video ", "likes": "2"},
            {"id": "1", "username": "a", "text": "Great video"},
            {"id": "2", "username": "b", "text": "Nice"},
        ], "https://www.tiktok.com/@u/video/9")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["text"], "Great video")
        self.assertIn("#comment-1", rows[0]["url"])

    def test_public_index_is_used_when_tiktok_search_is_empty(self):
        self.manager._fetch_html = Mock(return_value="<html></html>")
        self.manager._discover_tiktok_browser_urls = Mock(return_value=[])
        self.manager._discover_tiktok_index_urls = Mock(return_value=[
            "https://www.tiktok.com/@demo/video/123456"
        ])
        self.manager._tiktok_extract = Mock(side_effect=RuntimeError("blocked"))
        self.manager._tiktok_oembed_row = Mock(return_value={
            "title": "Demo video", "author": "Demo", "url": "https://www.tiktok.com/@demo/video/123456"
        })
        targets = self.manager._discover_tiktok_keyword_videos(
            "demo", {"max_items": 3, "proxies": []}
        )
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["prefill"]["title"], "Demo video")
        self.manager._discover_tiktok_index_urls.assert_called_once()

    def test_oembed_maps_preview_metadata(self):
        response = Mock()
        response.json.return_value = {
            "title": "A short video", "author_name": "Creator",
            "author_url": "https://www.tiktok.com/@creator",
            "thumbnail_url": "https://img.example/thumb.jpg",
        }
        self.manager._request = Mock(return_value=response)
        row = self.manager._tiktok_oembed_row(
            "https://www.tiktok.com/@creator/video/123", {"proxies": []}
        )
        self.assertEqual(row["username"], "creator")
        self.assertEqual(row["thumbnail"], "https://img.example/thumb.jpg")

    def test_dispatches_all_four_modes(self):
        self.manager._discover_tiktok_user = Mock(return_value=["user"])
        self.manager._discover_tiktok_account_videos = Mock(return_value=["videos"])
        self.manager._discover_tiktok_comments = Mock(return_value=["comments"])
        self.manager._discover_tiktok_keyword_videos = Mock(return_value=["keyword"])
        for mode in ("user", "videos", "comments", "keyword"):
            self.assertEqual(self.manager._discover_tiktok_data("value", {"tiktok_mode": mode}), [mode])

    def test_douyin_profile_search_accepts_at_prefixed_nickname_and_common_ids(self):
        rows = profile_rows({"user_list": [{
            "secUid": "sec-123", "nickname": "人民日报", "uniqueId": "rmrb",
            "follower_count": 7,
        }]}, "@人民日报")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["username"], "rmrb")
        self.assertEqual(rows[0]["followers"], 7)

    def test_douyin_profile_search_accepts_official_api_subdomains(self):
        self.assertTrue(_douyin_host("https://search.douyin.com/aweme/v1/web/search/user/"))
        self.assertFalse(_douyin_host("https://douyin.com.example.test/user/fake"))

    def test_douyin_profile_search_falls_back_to_exact_rendered_user_link(self):
        rows = profile_link_rows([
            {"href": "https://www.douyin.com/user/sec-wrong", "text": "人民日报健康客户端\n100万粉丝"},
            {"href": "https://www.douyin.com/user/sec-rmrb", "text": "人民日报\n抖音号：rmrb"},
        ], "@人民日报")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://www.douyin.com/user/sec-rmrb")


if __name__ == "__main__":
    unittest.main()
