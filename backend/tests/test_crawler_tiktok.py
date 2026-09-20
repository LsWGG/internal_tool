import json
import base64
import threading
import unittest
from unittest.mock import Mock, patch

from app.crawler_manager import CrawlerTaskManager
from app.douyin_profiles import _cookie_values, _douyin_host, _profile_query, aweme_comments, search_awemes, user_awemes, enrich_profile, profile_index_rows, profile_link_rows, profile_rows


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

    def test_douyin_profile_search_uses_only_exact_public_index_title(self):
        real_url = "https://www.douyin.com/user/sec-rmrb"
        bing_url = "https://www.bing.com/ck/a?u=a1" + base64.urlsafe_b64encode(real_url.encode()).decode().rstrip("=")
        rows = profile_index_rows([
            {"href": "https://www.douyin.com/user/sec-health", "text": "人民日报健康客户端 - 抖音"},
            {"href": bing_url, "text": "人民日报的抖音 - 抖音"},
        ], "人民日报")
        self.assertEqual([row["url"] for row in rows], [real_url])

    def test_douyin_keyword_search_flattens_aweme_info_and_paginates(self):
        pages = [
            {"data": [{"aweme_info": {"aweme_id": "1", "desc": "first", "author": {"nickname": "A", "unique_id": "a"}, "statistics": {"play_count": 2}}}], "has_more": 1, "cursor": 20},
            {"data": [{"aweme_info": {"aweme_id": "2", "desc": "second", "author": {"nickname": "B", "unique_id": "b"}, "statistics": {"digg_count": 3}}}], "has_more": 0, "cursor": 40},
        ]
        with patch('app.douyin_profiles._request_payload', side_effect=pages) as request:
            rows = search_awemes("demo", {"douyin_cookie": "x=1"}, 2)
        self.assertEqual([row["url"] for row in rows], ["https://www.douyin.com/video/1", "https://www.douyin.com/video/2"])
        self.assertEqual(rows[0]["views"], 2)
        self.assertEqual(rows[1]["likes"], 3)
        self.assertEqual(request.call_count, 2)

    def test_douyin_account_videos_use_sec_uid_cursor_pages(self):
        with patch('app.douyin_profiles._request_payload', return_value={
            "aweme_list": [{"aweme_id": "3", "desc": "post", "author": {"nickname": "人民日报"}}],
            "has_more": 0, "max_cursor": 0,
        }) as request:
            rows = user_awemes("sec-rmrb", {"douyin_cookie": "x=1"}, 10)
        self.assertEqual(rows[0]["url"], "https://www.douyin.com/video/3")
        self.assertEqual(request.call_args.args[0], "/aweme/v1/web/aweme/post/")
        self.assertEqual(request.call_args.args[1]["sec_user_id"], "sec-rmrb")

    def test_douyin_comments_map_and_page_with_cursor(self):
        with patch('app.douyin_profiles._request_payload', return_value={
            "comments": [{"cid": "9", "text": "很好", "create_time": 1700000000, "digg_count": 4,
                          "reply_comment_total": 1, "user": {"nickname": "用户", "unique_id": "user"}}],
            "has_more": 0, "cursor": 0,
        }) as request:
            rows = aweme_comments("123", "https://www.douyin.com/video/123", {"douyin_cookie": "x=1"}, 10)
        self.assertEqual(rows[0]["username"], "user")
        self.assertEqual(rows[0]["url"], "https://www.douyin.com/video/123#comment-9")
        self.assertEqual(request.call_args.args[0], "/aweme/v1/web/comment/list/")
        self.assertEqual(request.call_args.args[1]["aweme_id"], "123")

    def test_douyin_dispatches_each_mode_to_signed_flow(self):
        self.manager._discover_douyin_data = Mock(return_value=["douyin"])
        self.assertEqual(self.manager._discover_tiktok_data("x", {"source": "douyin", "tiktok_mode": "keyword"}), ["douyin"])
        self.manager._discover_douyin_data.assert_called_once()

    def test_douyin_cookie_enriches_profile_without_exposing_cookie(self):
        response = Mock()
        response.content = b'{}'
        response.json.return_value = {"user": {"sec_uid": "sec-rmrb", "unique_id": "rmrb", "nickname": "人民日报", "signature": "新闻", "verified": True, "avatar_larger": {"url_list": ["https://img/avatar"]}}, "user_stats": {"follower_count": 2, "following_count": 3, "aweme_count": 4, "total_favorited": 5}}
        row = {"username": "sec-rmrb", "display_name": "人民日报", "bio": "", "followers": "", "following": "", "videos": "", "likes": "", "verified": "", "avatar": "", "url": "https://www.douyin.com/user/sec-rmrb"}
        session = Mock()
        session.get.return_value = response
        with patch('app.douyin_profiles.requests.Session', return_value=session):
            enriched = enrich_profile(row, {"douyin_cookie": "sessionid=secret; msToken=token"})
        self.assertEqual((enriched["followers"], enriched["videos"], enriched["likes"]), (2, 4, 5))
        self.assertEqual(enriched["avatar"], "https://img/avatar")
        self.assertIn("X-Bogus=", session.get.call_args.args[0])
        self.assertNotIn("secret", session.get.call_args.args[0])
        session.close.assert_called_once()

    def test_douyin_profile_request_uses_browser_query_and_cookie_ms_token(self):
        cookies = _cookie_values("ttwid=abc; msToken=already-present; malformed")
        query = _profile_query("sec-rmrb", cookies)
        self.assertEqual(query["sec_user_id"], "sec-rmrb")
        self.assertEqual(query["msToken"], "already-present")
        self.assertEqual(query["channel"], "channel_pc_web")
        self.assertEqual(query["browser_name"], "Chrome")

    def test_create_keeps_douyin_cookie_out_of_task_record(self):
        manager = object.__new__(CrawlerTaskManager)
        manager.lock = threading.RLock()
        manager.tasks, manager.task_secrets, manager.controls = {}, {}, {}
        manager.pool = Mock()
        manager._save = Mock()
        task = manager.create({
            "source": "douyin", "tiktok_mode": "user", "keyword": "人民日报",
            "douyin_cookie": "sessionid=secret", "fields": [{"name": "url"}],
        })
        stored = manager.tasks[task["id"]]
        self.assertNotIn("douyin_cookie", stored["request"])
        self.assertEqual(manager.task_secrets[task["id"]]["douyin_cookie"], "sessionid=secret")
        self.assertNotIn("secret", json.dumps(stored, ensure_ascii=False))

    def test_douyin_cookie_is_removed_from_public_task_and_delete_memory(self):
        manager = object.__new__(CrawlerTaskManager)
        manager.lock = threading.RLock()
        manager.tasks = {"task": {"id": "task", "request": {"douyin_cookie": "secret"}}}
        manager.task_secrets = {"task": {"douyin_cookie": "secret"}}
        manager.controls = {"task": {"cancelled": False}}
        manager.data_dir = __import__("pathlib").Path("/tmp/no-task-dir")
        manager._save = Mock()
        public = manager.public_task(manager.tasks["task"])
        self.assertEqual(public["request"]["douyin_cookie"], "")
        self.assertTrue(public["request"]["douyin_cookie_configured"])
        self.assertTrue(manager.delete("task"))
        self.assertNotIn("task", manager.task_secrets)


if __name__ == "__main__":
    unittest.main()
