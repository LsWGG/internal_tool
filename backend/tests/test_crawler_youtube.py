import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from yt_dlp.utils import DownloadError

from app.crawler_adapters import ADAPTERS, SourceAdapter
from app.crawler_manager import CrawlerTaskManager


class YoutubeDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.manager = object.__new__(CrawlerTaskManager)
        self.manager.lock = threading.RLock()
        self.manager.active_runs = {}
        self.manager.controls = {}
        self.manager._save = MagicMock()
        self.manager._checkpoint = MagicMock()
        self.manager._update = MagicMock()

    def download(self):
        return self.manager._download_youtube_video(
            'https://www.youtube.com/watch?v=GE0pFiFJTKo',
            self.directory, 1, 1, {'youtube_quality': 720}, 'test',
        )

    def test_refreshes_expired_urls_and_uses_final_merged_file(self):
        # Larger orphaned tracks must never be chosen instead of the final output.
        (self.directory / '001_GE0pFiFJTKo_test.f140.m4a').write_bytes(b'a' * 1024)
        final = self.directory / '001_GE0pFiFJTKo_test.mp4'
        options_seen = []

        def factory(options):
            options_seen.append(options)
            downloader = MagicMock()
            downloader.__enter__.return_value = downloader
            if len(options_seen) < 3:
                downloader.extract_info.side_effect = DownloadError('HTTP Error 403: Forbidden')
            else:
                def finish(*args, **kwargs):
                    final.write_bytes(b'finished video')
                    options['post_hooks'][0](str(final))
                    return {'id': 'GE0pFiFJTKo'}
                downloader.extract_info.side_effect = finish
            return downloader

        with patch('yt_dlp.YoutubeDL', side_effect=factory), patch('app.crawler_manager.time.sleep'):
            _, result = self.download()
        self.assertEqual(len(options_seen), 3)
        self.assertEqual(result, final)
        self.assertFalse(options_seen[0]['skip_unavailable_fragments'])

    def test_permanent_failure_is_not_retried_and_ansi_is_removed(self):
        downloader = MagicMock()
        downloader.__enter__.return_value = downloader
        downloader.extract_info.side_effect = DownloadError('\x1b[0;31mERROR:\x1b[0m Private video')
        with patch('yt_dlp.YoutubeDL', return_value=downloader) as factory:
            with self.assertRaises(RuntimeError) as error:
                self.download()
        self.assertEqual(factory.call_count, 1)
        self.assertNotIn('\x1b', str(error.exception))

    def test_unfinished_tracks_are_not_a_success(self):
        (self.directory / '001_GE0pFiFJTKo_test.f398.mp4').write_bytes(b'unmerged')
        downloader = MagicMock()
        downloader.__enter__.return_value = downloader
        downloader.extract_info.return_value = {'id': 'GE0pFiFJTKo'}
        with patch('yt_dlp.YoutubeDL', return_value=downloader):
            with self.assertRaisesRegex(RuntimeError, '未生成完整文件'):
                self.download()

    def test_keyword_network_error_is_presented_without_transport_details(self):
        import requests
        self.manager._request = MagicMock(side_effect=requests.ConnectionError('dns unavailable'))
        with self.assertRaisesRegex(RuntimeError, 'YouTube 搜索暂时无法连接'):
            self.manager._discover_youtube_urls('OpenAI', {'max_items': 2})

    def test_youtube_modes_map_comments_user_and_account_videos(self):
        # 取出 yt-dlp 的那一层已经搬到 crawler_adapters.youtube，patch 目标随之改变。
        comment_info = {'comments': [{'id': 'c1', 'author': 'Alice', 'author_id': 'alice', 'text': 'Nice', 'like_count': 2}]}
        with patch('app.crawler_adapters.youtube.extract', return_value=comment_info):
            comments = self.manager._discover_youtube_data('https://youtu.be/GE0pFiFJTKo', {'youtube_mode': 'comments', 'max_items': 10})
        self.assertEqual(comments[0]['prefill']['username'], 'alice')
        with patch('app.crawler_adapters.youtube.extract', return_value={'channel_id': 'UC1', 'channel': 'Demo', 'channel_follower_count': 9}):
            user = self.manager._discover_youtube_data('@DemoChannel', {'youtube_mode': 'user', 'max_items': 10})
        self.assertEqual(user[0]['prefill']['followers'], 9)
        with patch('app.crawler_adapters.youtube.extract', return_value={'entries': [{'id': 'GE0pFiFJTKo', 'title': 'Video'}]}):
            videos = self.manager._discover_youtube_data('@DemoChannel', {'youtube_mode': 'videos', 'max_items': 10})
        self.assertEqual(videos[0]['url'], 'https://www.youtube.com/watch?v=GE0pFiFJTKo')

    def test_youtube_mode_fields_are_specific(self):
        self.assertIn('followers', [item['name'] for item in self.manager.builtin_fields('youtube', youtube_mode='user')])
        self.assertEqual([item['name'] for item in self.manager.builtin_fields('youtube', youtube_mode='comments')], ['author', 'username', 'text', 'published_at', 'likes', 'replies', 'url'])

    def test_retry_reuses_targets_but_next_scheduled_run_searches_again(self):
        task_dir = self.directory / 'test'
        task_dir.mkdir()
        url = 'https://www.youtube.com/watch?v=GE0pFiFJTKo'
        (task_dir / 'youtube_targets.json').write_text(json.dumps([url]))
        self.manager.data_dir = self.directory
        self.manager.tasks = {'test': {
            '_retry_youtube_targets': True,
            'request': {'source': 'youtube', 'keyword': 'Agent', 'fields': ['title'], 'delay_seconds': 0},
        }}
        self.manager.controls = {}
        self.manager._download_youtube_video = MagicMock(side_effect=RuntimeError('network unavailable'))
        # 关键词搜索已搬到 crawler_adapters.youtube，patch 目标随之改变。
        with patch('app.crawler_adapters.youtube.search_urls', return_value=[url]) as search_urls:
            self.manager._run('test')
            search_urls.assert_not_called()
            self.manager._run('test')
            search_urls.assert_called_once()


class AdapterProtocolTests(unittest.TestCase):
    """youtube 是第一个走适配器协议的源。

    这两条锁住协议的三个接缝：发现经适配器、网络回落到 manager._request、
    进度仍由 manager._update 上报 —— 现有测试里的 mock 全靠它们继续生效。
    """

    def setUp(self):
        self.manager = object.__new__(CrawlerTaskManager)

    def with_discover(self, discover):
        return patch.dict(ADAPTERS, {'youtube': SourceAdapter('youtube', 'YouTube', discover=discover)})

    def test_discovery_goes_through_the_adapter(self):
        seen = {}

        def fake_discover(value, ctx):
            seen.update(value=value, request=ctx.request, task_id=ctx.task_id)
            return [{'url': 'https://www.youtube.com/watch?v=GE0pFiFJTKo'}]

        with self.with_discover(fake_discover):
            targets = self.manager._discover_for('youtube', 'OpenAI', {'max_items': 1}, 'task-1')
        self.assertEqual(targets[0]['url'], 'https://www.youtube.com/watch?v=GE0pFiFJTKo')
        self.assertEqual((seen['value'], seen['task_id']), ('OpenAI', 'task-1'))
        self.assertEqual(seen['request'], {'max_items': 1})

    def test_adapter_http_and_progress_reach_the_manager(self):
        response = MagicMock()
        self.manager._request = MagicMock(return_value=response)
        self.manager._update = MagicMock()
        captured = {}

        def fake_discover(value, ctx):
            ctx.update(ctx.task_id, message='进行中')
            captured['html'] = ctx.http.get('https://www.youtube.com/results?q=x', [], timeout=30)
            return []

        with self.with_discover(fake_discover):
            self.assertEqual(self.manager._discover_for('youtube', 'OpenAI', {}, 'task-1'), [])
        self.assertEqual(captured['html'], response)
        self.manager._request.assert_called_once_with('https://www.youtube.com/results?q=x', [], timeout=30)
        self.manager._update.assert_called_once_with('task-1', message='进行中')


if __name__ == '__main__':
    unittest.main()
