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


class MediaOptionTests(unittest.TestCase):
    """下载选项要变成 yt-dlp 真正认的那些键。

    这里守的是一个很容易想当然的坑：yt-dlp 的 Python API **不会**根据 `extractaudio`
    这类旗标自己造 postprocessor（那是命令行包装层干的活）。所以 postprocessors 必须
    由我们自己拼，而且顺序是契约 —— 提取音频必须排在嵌入字幕和写元数据前面。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.manager = object.__new__(CrawlerTaskManager)
        self.manager.lock = threading.RLock()
        self.manager._checkpoint = MagicMock()
        self.manager._update = MagicMock()

    def options_for(self, request):
        """跑一次下载，把交给 YoutubeDL 的选项原样取回来。"""
        seen = []

        def factory(options):
            seen.append(options)
            downloader = MagicMock()
            downloader.__enter__.return_value = downloader

            def finish(*args, **kwargs):
                path = self.directory / '001_GE0pFiFJTKo_clip.mp4'
                path.write_bytes(b'video')
                options['post_hooks'][0](str(path))
                return {'id': 'GE0pFiFJTKo'}

            downloader.extract_info.side_effect = finish
            return downloader

        with patch('yt_dlp.YoutubeDL', side_effect=factory):
            self.manager._download_youtube_video(
                'https://www.youtube.com/watch?v=GE0pFiFJTKo',
                self.directory, 1, 1, dict(request), 'test',
            )
        return seen[0]

    def test_defaults_keep_today_behaviour(self):
        options = self.options_for({'youtube_quality': 720})
        self.assertNotIn('postprocessors', options)
        self.assertNotIn('writesubtitles', options)
        self.assertNotIn('writethumbnail', options)
        self.assertNotIn('download_archive', options)
        self.assertEqual(options['merge_output_format'], 'mp4')

    def test_postprocessors_are_in_the_order_the_cli_uses(self):
        options = self.options_for({
            'download_mode': 'audio_m4a', 'subtitle_mode': 'embed',
            'thumbnail_mode': 'embed', 'write_metadata': True,
        })
        self.assertEqual([item['key'] for item in options['postprocessors']],
                         ['FFmpegExtractAudio', 'FFmpegEmbedSubtitle',
                          'FFmpegMetadata', 'EmbedThumbnail'])
        self.assertEqual(options['postprocessors'][0]['preferredcodec'], 'm4a')

    def test_embed_also_turns_on_the_sidecar_write_it_depends_on(self):
        # 嵌入读的是 info['requested_subtitles'][lang]['filepath']，那个字段只有写了
        # 字幕文件之后才存在 —— 只加 postprocessor 会静默地什么都不嵌。
        options = self.options_for({'subtitle_mode': 'embed'})
        self.assertTrue(options['writesubtitles'])
        self.assertIn('FFmpegEmbedSubtitle', [item['key'] for item in options['postprocessors']])
        self.assertTrue(self.options_for({'thumbnail_mode': 'embed'})['writethumbnail'])

    def test_sidecar_subtitles_need_no_transcoding(self):
        options = self.options_for({'subtitle_mode': 'sidecar'})
        self.assertTrue(options['writesubtitles'])
        self.assertEqual(options['subtitlesformat'], 'vtt/srt/best')
        self.assertEqual(options['subtitleslangs'], ['zh-Hans', 'zh-Hant', 'zh', 'en'])
        self.assertNotIn('postprocessors', options)

    def test_audio_mode_does_not_keep_the_mp4_merge(self):
        # 留着 merge_output_format 会把音频容器也往 mp4 上掰。
        options = self.options_for({'download_mode': 'audio_mp3'})
        self.assertEqual(options['format'], 'bestaudio/best')
        self.assertNotIn('merge_output_format', options)

    def test_archive_lives_beside_the_videos_it_describes(self):
        options = self.options_for({'skip_downloaded': True})
        self.assertEqual(Path(options['download_archive']),
                         self.directory / 'download_archive.txt')

    def test_mp3_cannot_embed_subtitles_so_it_degrades_to_sidecar(self):
        # FFmpegEmbedSubtitlePP 不认识 mp3，命中时只打一行提示就返回：字幕既没嵌进去，
        # 还会留下一地的旁挂文件。降级成旁挂才是用户真正想要的结果。
        options = self.options_for({'download_mode': 'audio_mp3', 'subtitle_mode': 'embed'})
        self.assertNotIn('FFmpegEmbedSubtitle', [item.get('key') for item in options.get('postprocessors', [])])
        self.assertTrue(options['writesubtitles'])


class DownloadArchiveTests(unittest.TestCase):
    """归档命中时不能把别人的视频挂到这一行上。

    yt-dlp 命中 download_archive 会直接返回：不下载、不跑 postprocessor，因此
    post_hooks 一次都不触发、completed_paths 是空的。如果没有针对性处理，紧接着那条
    「扫描输出目录」的兜底逻辑就会从 videos/ 里随便挑一个文件 —— 结果是一行数据配着
    另一个视频，而且在报表上看不出来。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.manager = object.__new__(CrawlerTaskManager)
        self.manager.lock = threading.RLock()
        self.manager._checkpoint = MagicMock()
        self.manager._update = MagicMock()

    def run_download(self, archive_lines, files):
        for name, size in files.items():
            (self.directory / name).write_bytes(b'x' * size)
        (self.directory / 'download_archive.txt').write_text(
            ''.join(line + '\n' for line in archive_lines), 'utf-8')
        calls = []

        def factory(options):
            calls.append(options)
            downloader = MagicMock()
            downloader.__enter__.return_value = downloader

            def skip(*args, **kwargs):
                # 归档命中：post_hooks 不触发，completed_paths 保持为空。
                return {'id': 'GE0pFiFJTKo'}

            downloader.extract_info.side_effect = skip
            return downloader

        with patch('yt_dlp.YoutubeDL', side_effect=factory):
            _, path = self.manager._download_youtube_video(
                'https://www.youtube.com/watch?v=GE0pFiFJTKo',
                self.directory, 1, 1, {'skip_downloaded': True}, 'test',
            )
        return path, calls

    def test_archive_hit_resolves_by_video_id_not_by_directory_scan(self):
        path, _ = self.run_download(
            ['youtube GE0pFiFJTKo'],
            {'001_GE0pFiFJTKo_wanted.mp4': 512, '002_OTHERVIDEO_other.mp4': 4096},
        )
        self.assertEqual(path.name, '001_GE0pFiFJTKo_wanted.mp4')

    def test_archive_hit_never_borrows_another_videos_file(self):
        # 目录里只有别人的视频时，宁可按失败上报，也不能把那个文件挂到这一行上 ——
        # 那样出来的是一行数据配一段无关影像，报表上完全看不出来。
        with patch('app.crawler_manager.is_ffmpeg_available', return_value=True):
            with self.assertRaisesRegex(RuntimeError, '未生成完整文件'):
                self.run_download(['youtube GE0pFiFJTKo'],
                                  {'002_OTHERVIDEO_other.mp4': 4096})

    def test_archive_entry_is_dropped_and_redownloaded_when_the_file_is_gone(self):
        # 归档说下过了，但文件已经不在了。不删那一行的话，yt-dlp 会照着归档再跳过一遍，
        # 永远补不回来。
        (self.directory / 'download_archive.txt').write_text(
            'youtube OTHERVIDEO\nyoutube GE0pFiFJTKo\n', 'utf-8')
        calls, archive_now = [], []

        def factory(options):
            calls.append(options)
            downloader = MagicMock()
            downloader.__enter__.return_value = downloader

            def attempt(*args, **kwargs):
                if len(calls) == 1:
                    # 第一次照归档跳过：不下载、不触发 post_hooks。
                    return {'id': 'GE0pFiFJTKo'}
                # 重下之前那一行必须已经删掉了 —— 留着的话 yt-dlp 会照它再跳过一次。
                archive_now.append(
                    (self.directory / 'download_archive.txt').read_text('utf-8'))
                path = self.directory / '001_GE0pFiFJTKo_recovered.mp4'
                path.write_bytes(b'video')
                options['post_hooks'][0](str(path))
                return {'id': 'GE0pFiFJTKo'}

            downloader.extract_info.side_effect = attempt
            return downloader

        with patch('yt_dlp.YoutubeDL', side_effect=factory):
            _, path = self.manager._download_youtube_video(
                'https://www.youtube.com/watch?v=GE0pFiFJTKo',
                self.directory, 1, 1, {'skip_downloaded': True}, 'test',
            )
        self.assertEqual(path.name, '001_GE0pFiFJTKo_recovered.mp4')
        self.assertEqual(len(calls), 2, '归档行删掉后应该重新下载一次')
        # 只删这一条，别的视频的记录不能被牵连。
        self.assertEqual(archive_now, ['youtube OTHERVIDEO\n'])

    def test_audio_mode_does_not_pick_up_a_video_file_by_id(self):
        # 同一个 id 下的旧 .mp4 残留不属于这次要的 mp3，按 id 找回时也不能顺手拿它。
        (self.directory / '001_GE0pFiFJTKo_clip.mp4').write_bytes(b'x' * 512)
        (self.directory / 'download_archive.txt').write_text('youtube GE0pFiFJTKo\n', 'utf-8')
        downloader = MagicMock()
        downloader.__enter__.return_value = downloader
        downloader.extract_info.return_value = {'id': 'GE0pFiFJTKo'}

        def factory(options):
            return downloader

        with patch('yt_dlp.YoutubeDL', side_effect=factory):
            with self.assertRaisesRegex(RuntimeError, '未生成完整文件'):
                self.manager._download_youtube_video(
                    'https://www.youtube.com/watch?v=GE0pFiFJTKo',
                    self.directory, 1, 1,
                    {'download_mode': 'audio_mp3', 'skip_downloaded': True}, 'test',
                )


class MediaOptionValidationTests(unittest.TestCase):
    """创建任务时就要把「选了 ffmpeg 才能做的选项、但环境里没有 ffmpeg」拦下来。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = CrawlerTaskManager(Path(self.temp.name))

    def payload(self, **extra):
        # 用 cron 而不是「立即执行」：create 对定时任务只登记、不提交线程池，
        # 这几条用例就不会真去联网，也不会留一个还在写 task_dir 的线程跟临时目录清理抢。
        return {'source': 'ytdlp', 'ytdlp_mode': 'videos', 'urls': ['https://vimeo.com/123456'],
                'download_videos': True, 'fields': ['title'], 'cron': '0 3 * * *', **extra}

    def test_ffmpeg_dependent_options_are_rejected_with_an_actionable_message(self):
        with patch('app.crawler_manager.is_ffmpeg_available', return_value=False):
            with self.assertRaises(ValueError) as error:
                self.manager.create(self.payload(download_mode='audio_mp3'))
        self.assertIn('ffmpeg', str(error.exception))

    def test_sidecar_only_options_still_work_without_ffmpeg(self):
        with patch('app.crawler_manager.is_ffmpeg_available', return_value=False):
            task = self.manager.create(self.payload(
                subtitle_mode='sidecar', thumbnail_mode='sidecar', write_metadata=False))
        self.assertEqual(task['request']['subtitle_mode'], 'sidecar')
        self.assertFalse(task['request']['write_metadata'])

    def test_turning_off_download_drops_the_media_options(self):
        task = self.manager.create({'source': 'ytdlp', 'ytdlp_mode': 'videos',
                                    'urls': ['https://vimeo.com/123456'],
                                    'download_videos': False, 'fields': ['title'],
                                    'cron': '0 3 * * *',
                                    'download_mode': 'audio_mp3', 'subtitle_mode': 'embed'})
        self.assertEqual(task['request']['download_mode'], 'video')
        self.assertEqual(task['request']['subtitle_mode'], 'none')
        self.assertFalse(task['request']['skip_downloaded'])


class SidecarPackagingTests(unittest.TestCase):
    """旁挂的字幕和封面要跟着视频一起进 ZIP，半截的临时文件则不能。"""

    def test_sidecar_files_share_the_stem_and_reject_partials(self):
        from app.crawler_manager import _sidecar_files
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            media = directory / '001_GE0pFiFJTKo_Song [Official Video].mp4'
            media.write_bytes(b'video')
            for extra in ('001_GE0pFiFJTKo_Song [Official Video].en.vtt',
                          '001_GE0pFiFJTKo_Song [Official Video].zh-Hans.vtt',
                          '001_GE0pFiFJTKo_Song [Official Video].webp',
                          '001_GE0pFiFJTKo_Song [Official Video].mp4.part',
                          '001_GE0pFiFJTKo_Song [Official Video].ytdl',
                          '001_GE0pFiFJTKo_Song [Official Video].info.json'):
                (directory / extra).write_bytes(b'x')
            (directory / '002_OTHERVIDEO_other.en.vtt').write_bytes(b'x')
            found = sorted(path.name for path in _sidecar_files(media))
        self.assertEqual(found, [
            '001_GE0pFiFJTKo_Song [Official Video].en.vtt',
            '001_GE0pFiFJTKo_Song [Official Video].webp',
            '001_GE0pFiFJTKo_Song [Official Video].zh-Hans.vtt',
        ])


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
