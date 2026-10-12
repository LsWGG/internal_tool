"""yt-dlp 通用视频源：链接收集、合集展开、元数据预览与整条下载打包链路。

yt-dlp 本身不在这里跑：真正调用它的 `extract` 被替换掉，网络解析不参与这段测试。
"""
import importlib.util
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.crawler_adapters import get_adapter, ytdlp
from app.crawler_manager import CrawlerTaskManager


class TargetUrlTests(unittest.TestCase):
    def test_extracts_dedupes_and_keeps_order(self):
        urls = ytdlp.target_urls("https://a.com/1\nhttps://b.com/2\nhttps://a.com/1",
                                 {"urls": ["https://c.com/3"]})
        self.assertEqual(urls, ["https://a.com/1", "https://b.com/2", "https://c.com/3"])

    def test_trailing_punctuation_and_full_width_commas_are_trimmed(self):
        self.assertEqual(ytdlp.target_urls("见 https://a.com/1。", {}), ["https://a.com/1"])
        self.assertEqual(ytdlp.target_urls("https://a.com/1，https://b.com/2", {}),
                         ["https://a.com/1", "https://b.com/2"])


class EntryUrlTests(unittest.TestCase):
    def test_prefers_the_full_url(self):
        self.assertEqual(ytdlp._entry_url({"webpage_url": "https://x/1", "url": "https://y/2"}, {}),
                         "https://x/1")

    def test_youtube_video_id_is_rebuilt_into_a_watch_url(self):
        self.assertEqual(ytdlp._entry_url({"id": "GE0pFiFJTKo"}, {"extractor_key": "Youtube"}),
                         "https://www.youtube.com/watch?v=GE0pFiFJTKo")

    def test_unresolvable_entry_is_skipped_instead_of_guessed(self):
        # 还原不出完整地址的条目宁可跳过：猜一个不存在的地址只会变成一条失败记录。
        self.assertEqual(ytdlp._entry_url({"id": "abc"}, {"extractor_key": "Generic"}), "")


class DiscoverTests(unittest.TestCase):
    def context(self, request, task_id="task-1"):
        ctx = MagicMock()
        ctx.request = request
        ctx.task_id = task_id
        return ctx

    def test_empty_input_raises(self):
        with self.assertRaises(ValueError):
            ytdlp.discover("", self.context({"ytdlp_mode": "videos", "max_items": 10, "urls": []}))

    def test_videos_mode_returns_links_verbatim(self):
        ctx = self.context({"ytdlp_mode": "videos", "max_items": 10, "urls": []})
        self.assertEqual(ytdlp.discover("https://a.com/1", ctx), ["https://a.com/1"])

    def test_playlist_mode_expands_and_honours_max_items(self):
        playlist = {"extractor_key": "Youtube",
                    "entries": [{"id": "aaaaaaaaaaa"}, {"id": "bbbbbbbbbbb"}, {"id": "ccccccccccc"}]}
        ctx = self.context({"ytdlp_mode": "playlist", "max_items": 2, "urls": []})
        with patch.object(ytdlp, "extract", return_value=playlist):
            self.assertEqual(ytdlp.discover("https://www.youtube.com/playlist?list=X", ctx),
                             ["https://www.youtube.com/watch?v=aaaaaaaaaaa",
                              "https://www.youtube.com/watch?v=bbbbbbbbbbb"])

    def test_playlist_without_entries_raises(self):
        ctx = self.context({"ytdlp_mode": "playlist", "max_items": 10, "urls": []})
        with patch.object(ytdlp, "extract", return_value={"extractor_key": "Youtube", "entries": []}):
            with self.assertRaises(RuntimeError):
                ytdlp.discover("https://www.youtube.com/playlist?list=X", ctx)

    def test_download_disabled_returns_metadata_rows_instead_of_links(self):
        info = {"title": "标题", "webpage_url": "https://a.com/1", "duration": 12}
        ctx = self.context({"ytdlp_mode": "videos", "max_items": 10, "urls": [],
                            "download_videos": False})
        with patch.object(ytdlp, "extract", return_value=info):
            targets = ytdlp.discover("https://a.com/1", ctx)
        self.assertEqual(targets[0]["prefill"]["title"], "标题")
        self.assertEqual(targets[0]["prefill"]["duration"], 12)
        self.assertEqual(targets[0]["url"], "https://a.com/1")

    def test_preview_targets_carry_metadata_for_the_table(self):
        info = {"title": "标题", "webpage_url": "https://a.com/1", "duration": 12}
        with patch.object(ytdlp, "extract", return_value=info):
            rows = ytdlp.preview_targets({"ytdlp_mode": "videos", "max_items": 3},
                                         ["https://a.com/1"], 3)
        self.assertEqual(rows[0]["prefill"]["title"], "标题")
        self.assertEqual(rows[0]["prefill"]["duration"], 12)
        self.assertEqual(rows[0]["url"], "https://a.com/1")

    def test_resolve_targets_leaves_plain_links_untouched(self):
        # videos 模式绝不能因为解析而多一次网络请求。
        with patch.object(ytdlp, "extract") as extract:
            self.assertEqual(ytdlp.resolve_targets({"ytdlp_mode": "videos"}, ["https://a.com/1"], 5),
                             ["https://a.com/1"])
        extract.assert_not_called()


class DouyinShareLinkTests(unittest.TestCase):
    """抖音分享链接（频道页 + modal_id）不能原样丢给 yt-dlp：它的 Douyin 解析器只认 /video/<id>，
    落到 Generic 就是一句「Unsupported URL」。"""

    MODAL = "https://www.douyin.com/jingxuan?modal_id=7693559649655672118"

    def test_modal_link_is_rewritten_to_the_video_page(self):
        self.assertEqual(ytdlp.normalize_url(self.MODAL),
                         "https://www.douyin.com/video/7693559649655672118")

    def test_modal_id_is_found_behind_other_parameters(self):
        self.assertEqual(ytdlp.normalize_url("https://www.douyin.com/user/MS4wLjAB?from=x&modal_id=123"),
                         "https://www.douyin.com/video/123")

    def test_urls_without_modal_id_are_left_alone(self):
        for url in ("https://www.bilibili.com/video/BV1xx411c7mD",
                    "https://www.douyin.com/video/111",
                    "https://www.douyin.com/jingxuan"):
            self.assertEqual(ytdlp.normalize_url(url), url)

    def test_discovery_normalizes_before_handing_links_to_yt_dlp(self):
        ctx = MagicMock()
        ctx.request = {"ytdlp_mode": "videos", "max_items": 10, "urls": [self.MODAL]}
        with patch.object(ytdlp, "extract") as extract:
            self.assertEqual(ytdlp.discover("", ctx),
                             ["https://www.douyin.com/video/7693559649655672118"])
        extract.assert_not_called()


class LinkHintTests(unittest.TestCase):
    """账号主页是最好认错的一类链接：yt-dlp 只会回一句 Unsupported URL，
    用户看不出是链接形态不对，也不知道本系统另有能处理它的来源。"""

    def test_x_profile_gets_a_hint_pointing_at_the_twitter_source(self):
        for url in ("https://x.com/realDonaldTrump",
                    "https://twitter.com/realDonaldTrump",
                    "https://www.x.com/OpenAI/"):
            hint = ytdlp.link_hint(url)
            self.assertIn("推特 / X", hint, url)

    def test_single_tweets_are_not_treated_as_profiles(self):
        # 单条推文是 yt-dlp 真正支持的形态，不该被劝去换来源。
        for url in ("https://x.com/i/web/status/123", "https://x.com/user/status/123456789"):
            self.assertEqual(ytdlp.link_hint(url), "", url)

    def test_built_in_timeline_pages_are_not_handles(self):
        for url in ("https://x.com/home", "https://x.com/explore", "https://x.com/settings"):
            self.assertEqual(ytdlp.link_hint(url), "", url)

    def test_other_sites_get_no_guess(self):
        for url in ("https://www.bilibili.com/video/BV1xx411c7mD",
                    "https://www.youtube.com/@someone",
                    "https://www.douyin.com/video/111"):
            self.assertEqual(ytdlp.link_hint(url), "", url)


@unittest.skipUnless(importlib.util.find_spec("yt_dlp"), "yt-dlp 未安装")
class UnsupportedUrlMessageTests(unittest.TestCase):
    def test_profile_error_carries_the_hint(self):
        import yt_dlp
        with patch.object(yt_dlp, "YoutubeDL",
                          side_effect=yt_dlp.utils.DownloadError(
                              "ERROR: Unsupported URL: https://x.com/realDonaldTrump")):
            with self.assertRaises(RuntimeError) as caught:
                ytdlp.extract("https://x.com/realDonaldTrump", {})
        self.assertIn("推特 / X", str(caught.exception))

    def test_other_errors_are_left_alone(self):
        # 风控、超时、地区限制的下一步各不相同，套同一句建议只会把人带偏。
        import yt_dlp
        with patch.object(yt_dlp, "YoutubeDL",
                          side_effect=yt_dlp.utils.DownloadError(
                              "ERROR: [Douyin] 769: Fresh cookies are needed")):
            with self.assertRaises(RuntimeError) as caught:
                ytdlp.extract("https://x.com/realDonaldTrump", {})
        self.assertNotIn("推特 / X", str(caught.exception))

    def test_hint_is_not_repeated_for_pages_yt_dlp_does_support(self):
        import yt_dlp
        with patch.object(yt_dlp, "YoutubeDL",
                          side_effect=yt_dlp.utils.DownloadError(
                              "ERROR: Unsupported URL: https://www.youtube.com/@someone")):
            with self.assertRaises(RuntimeError) as caught:
                ytdlp.extract("https://www.youtube.com/@someone", {})
        self.assertIn("Unsupported URL", str(caught.exception))
        self.assertNotIn("推特 / X", str(caught.exception))


class ErrorFormattingTests(unittest.TestCase):
    """用户看到的是异常文本本身，终端配色和 `ERROR:` 前缀必须在这里剥掉。"""

    def test_ansi_colour_and_error_prefix_are_stripped(self):
        self.assertEqual(
            ytdlp.clean_error("\x1b[0;31mERROR:\x1b[0m Unsupported URL: https://x"),
            "Unsupported URL: https://x")

    def test_only_the_last_non_empty_line_survives(self):
        self.assertEqual(ytdlp.clean_error("\n  WARNING: 无关噪声\nERROR: 真正的错误\n\n"),
                         "真正的错误")

    def test_blank_message_yields_nothing(self):
        self.assertEqual(ytdlp.clean_error("\x1b[0;31m\x1b[0m"), "")

    def test_message_is_capped(self):
        self.assertLessEqual(len(ytdlp.clean_error("x" * 500)), 240)


@unittest.skipUnless(importlib.util.find_spec("yt_dlp"), "yt-dlp 未安装")
class CookieInstallTests(unittest.TestCase):
    def test_cookies_land_in_the_jar_the_douyin_extractor_reads(self):
        # DouyinIE 判断登录态读的是 cookie jar，不是 http_headers，所以必须真的写进 jar。
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True}) as downloader:
            ytdlp.install_cookies(downloader, "sessionid=abc123; ttwid=def456")
            jar = {cookie.name: cookie for cookie in downloader.cookiejar}
        self.assertEqual(set(jar), {"sessionid", "ttwid"})
        self.assertEqual(jar["sessionid"].value, "abc123")
        self.assertEqual(jar["sessionid"].domain, "douyin.com")


class SupportedSitesTests(unittest.TestCase):
    """「支持哪些网站」清单：从本机 yt-dlp 的解析器注册表现算，不联网。"""

    def setUp(self):
        # 清单是进程级缓存，逐个用例清掉，免得互相看见对方的结果。
        self.addCleanup(setattr, ytdlp, "_SITES_CACHE", None)
        ytdlp._SITES_CACHE = None

    def test_lists_many_sites_with_the_ones_people_actually_use(self):
        data = ytdlp.supported_sites()
        self.assertTrue(data["available"])
        self.assertGreater(data["count"], 500)
        self.assertEqual(data["count"], len(data["sites"]))
        by_name = {item["name"]: item for item in data["sites"]}
        self.assertIn("youtube.com", by_name["youtube"]["domains"])
        self.assertIn("bilibili.com", by_name["BiliBili"]["domains"])
        self.assertEqual(by_name["XiaoHongShu"]["label"], "小红书")

    def test_site_names_are_unique_and_rows_read_alphabetically(self):
        sites = ytdlp.supported_sites()["sites"]
        names = [item["name"] for item in sites]
        self.assertEqual(len(names), len(set(names)))
        # 排序按页面上显示的那一列（说明文字优先，没有说明才用名字）。
        labels = [(item["label"] or item["name"]).lower() for item in sites]
        self.assertEqual(labels, sorted(labels))

    def test_official_addresses_are_handed_to_the_page(self):
        data = ytdlp.supported_sites()
        self.assertEqual(data["official_url"], "https://github.com/yt-dlp/yt-dlp")
        self.assertTrue(data["official_sites_url"].startswith("https://github.com/yt-dlp/yt-dlp/"))

    def test_popular_sites_carry_a_name_and_a_domain(self):
        popular = ytdlp.supported_sites()["popular"]
        self.assertGreaterEqual(len(popular), 10)
        for item in popular:
            self.assertTrue(item["label"])
            self.assertIn(".", item["domain"])

    def test_public_suffixes_are_not_mistaken_for_site_domains(self):
        # 10play 的正则是 `10(?:play)?\.com\.au`，只按字面扫会扫出 `com.au`。
        self.assertNotIn("com.au", ytdlp.supported_sites()["sites"][0]["domains"])

    def test_result_is_cached_between_calls(self):
        first = ytdlp.supported_sites()
        self.assertIs(ytdlp.supported_sites(), first)

    def test_missing_yt_dlp_is_reported_instead_of_raising(self):
        with patch.dict(sys.modules, {"yt_dlp.extractor": None}):
            data = ytdlp.supported_sites()
        self.assertFalse(data["available"])
        self.assertEqual(data["count"], 0)
        self.assertIn("yt-dlp", data["message"])
        # 失败的结果不进缓存，装好之后还能拿到真正的清单。
        self.assertIsNone(ytdlp._SITES_CACHE)


class AdapterRegistrationTests(unittest.TestCase):
    def test_registered_as_a_video_source_without_keyword(self):
        adapter = get_adapter("ytdlp")
        self.assertEqual(adapter.name, "ytdlp")
        self.assertEqual(adapter.modes, ("videos", "playlist"))
        self.assertTrue(adapter.supports_video)
        self.assertFalse(adapter.requires_keyword)

    def test_mode_is_validated(self):
        with self.assertRaises(ValueError):
            get_adapter("ytdlp").validate({"ytdlp_mode": "keyword"})

    def test_fields_match_youtube_so_download_fill_can_be_reused(self):
        self.assertEqual([item["name"] for item in CrawlerTaskManager.builtin_fields("ytdlp")],
                         [item["name"] for item in CrawlerTaskManager.builtin_fields("youtube")])


class YtdlpRunTests(unittest.TestCase):
    """整条任务链：发现 → 下载 → 填表 → 打包。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.manager = object.__new__(CrawlerTaskManager)
        self.manager.data_dir = self.directory
        self.manager.lock = threading.RLock()
        self.manager.controls = {}
        self.manager.active_runs = {}
        self.manager._save = MagicMock()
        self.manager._start_run = MagicMock(return_value="run-1")
        self.manager._checkpoint = MagicMock()
        self.manager._update = MagicMock()
        self.manager._append_cumulative = MagicMock(return_value={})
        self.manager._archive_run_result = MagicMock(side_effect=lambda task_id, run_id, result: result)

    def run_task(self, request):
        self.manager.tasks = {"task-1": {"id": "task-1", "name": "yt-dlp 视频采集",
                                         "request": request, "runs": []}}
        with patch("app.crawler_manager.time.sleep"):
            self.manager._run("task-1")
        return self.manager._archive_run_result.call_args[0][2]

    def fake_download(self, videos_dir):
        def download(url, directory, index, total, request, task_id):
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{index:03d}_abc_Demo.mp4"
            path.write_bytes(b"video")
            return {"title": "Demo", "webpage_url": url, "duration": 42, "channel": "Someone"}, path
        return download

    def test_download_run_packages_videos_under_a_generic_zip_name(self):
        videos_dir = self.directory / "task-1" / "videos"
        self.manager._download_youtube_video = self.fake_download(videos_dir)
        result = self.run_task({
            "source": "ytdlp", "ytdlp_mode": "videos", "urls": ["https://a.com/1"],
            "fields": ["title", "url", "duration"], "output_format": "xlsx",
            "download_videos": True, "max_items": 10, "delay_seconds": 0, "proxies": [],
        })
        self.assertEqual(result["file"], "videos.zip")
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["video_count"], 1)
        with zipfile.ZipFile(self.directory / "task-1" / "videos.zip") as package:
            names = package.namelist()
        self.assertIn("data.xlsx", names)
        self.assertIn("videos/001_abc_Demo.mp4", names)

    def test_sidecar_subtitles_and_thumbnails_ride_along_in_the_zip(self):
        # 字幕和封面是旁挂文件，不进数据表的任何一列，只能靠和视频共用的文件名前缀找回来。
        # 这里锁的是「它们真的进了包」以及「半截的临时文件没进包」。
        videos_dir = self.directory / "task-1" / "videos"
        base = self.fake_download(videos_dir)

        def download(url, directory, index, total, request, task_id):
            info, path = base(url, directory, index, total, request, task_id)
            # 真实下载器会把字幕和封面写在媒体文件旁边；打包是在任务收尾时做的。
            for name in ("en.vtt", "zh-Hans.vtt", "webp", "mp4.part"):
                (directory / f"{path.stem}.{name}").write_bytes(b"sidecar")
            return info, path

        self.manager._download_youtube_video = download
        result = self.run_task({
            "source": "ytdlp", "ytdlp_mode": "videos", "urls": ["https://a.com/1"],
            "fields": ["title", "url"], "output_format": "xlsx",
            "download_videos": True, "max_items": 10, "delay_seconds": 0, "proxies": [],
            "subtitle_mode": "sidecar", "thumbnail_mode": "sidecar",
        })
        with zipfile.ZipFile(self.directory / "task-1" / "videos.zip") as package:
            names = package.namelist()
        self.assertIn("videos/001_abc_Demo.en.vtt", names)
        self.assertIn("videos/001_abc_Demo.zh-Hans.vtt", names)
        self.assertIn("videos/001_abc_Demo.webp", names)
        self.assertNotIn("videos/001_abc_Demo.mp4.part", names)
        # 旁挂文件不能算进「视频数」：报了 5 个视频而实际只有 1 个，任务记录就不可信了。
        self.assertEqual(result["video_count"], 1)

    def test_row_carries_metadata_and_the_packaged_file_name(self):
        videos_dir = self.directory / "task-1" / "videos"
        self.manager._download_youtube_video = self.fake_download(videos_dir)
        self.run_task({
            "source": "ytdlp", "ytdlp_mode": "videos", "urls": ["https://a.com/1"],
            "fields": ["title", "url", "duration"], "output_format": "json",
            "download_videos": True, "max_items": 10, "delay_seconds": 0, "proxies": [],
        })
        row = self.manager._append_cumulative.call_args[0][2][0]
        self.assertEqual(row["title"], "Demo")
        self.assertEqual(row["duration"], 42)
        self.assertEqual(row["url"], "https://a.com/1")
        # video_file 不在勾选字段里，但下载任务必须留着它，否则 ZIP 里找不到对应关系。
        self.assertEqual(row["video_file"], "videos/001_abc_Demo.mp4")

    def test_playlist_mode_downloads_every_expanded_entry(self):
        videos_dir = self.directory / "task-1" / "videos"
        downloader = self.fake_download(videos_dir)
        seen = []

        def download(url, directory, index, total, request, task_id):
            seen.append(url)
            return downloader(url, directory, index, total, request, task_id)

        self.manager._download_youtube_video = download
        playlist = {"extractor_key": "Youtube", "entries": [{"id": "aaaaaaaaaaa"}, {"id": "bbbbbbbbbbb"}]}
        with patch.object(ytdlp, "extract", return_value=playlist):
            result = self.run_task({
                "source": "ytdlp", "ytdlp_mode": "playlist",
                "urls": ["https://www.youtube.com/playlist?list=X"],
                "fields": ["title", "url"], "output_format": "json",
                "download_videos": True, "max_items": 10, "delay_seconds": 0, "proxies": [],
            })
        self.assertEqual(seen, ["https://www.youtube.com/watch?v=aaaaaaaaaaa",
                                "https://www.youtube.com/watch?v=bbbbbbbbbbb"])
        self.assertEqual(result["video_count"], 2)

    def test_task_without_download_flag_collects_metadata_only(self):
        self.manager._download_youtube_video = MagicMock()
        info = {"title": "只采元数据", "webpage_url": "https://a.com/1", "duration": 7}
        with patch.object(ytdlp, "extract", return_value=info):
            result = self.run_task({
                "source": "ytdlp", "ytdlp_mode": "videos", "urls": ["https://a.com/1"],
                "fields": ["title", "url", "duration"], "output_format": "json",
                "download_videos": False, "max_items": 10, "delay_seconds": 0, "proxies": [],
            })
        self.manager._download_youtube_video.assert_not_called()
        self.assertEqual(result["file"], "data.json")
        row = self.manager._append_cumulative.call_args[0][2][0]
        self.assertEqual(row["title"], "只采元数据")
        self.assertEqual(row["duration"], 7)


if __name__ == "__main__":
    unittest.main()
