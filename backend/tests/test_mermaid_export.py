import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from app.mermaid_manager import MermaidExportManager

MARKDOWN = b'''# Document
## First chart
```mermaid
flowchart LR
A --> B
```
## Second chart
```mermaid
flowchart LR
C --> D
```
'''

class MermaidExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = MermaidExportManager(Path(self.temp.name))
        self.task = self.manager.preview('charts.md', MARKDOWN)
        self.calls = []

    def tearDown(self):
        self.manager.pool.shutdown(wait=True)
        self.temp.cleanup()

    def fake_render(self, task, block, output, scale):
        self.calls.append((task['theme'], block['index'], scale))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(f"{task['theme']}:{task['custom']}:{scale}")

    def drain(self):
        self.manager.pool.submit(lambda: None).result(timeout=5)

    def test_parse_only_and_lazy_preview(self):
        self.assertEqual([r['title'] for r in self.task['images']], ['First chart', 'Second chart'])
        self.assertTrue(all(r['status'] == 'idle' and 'url' not in r for r in self.task['images']))
        self.assertEqual(list(Path(self.temp.name).rglob('*.png')), [])
        with patch.object(self.manager, '_render_png', side_effect=self.fake_render):
            self.manager.start_preview_render(self.task['id'], 2)
            self.drain()
            self.manager.start_preview_render(self.task['id'], 2)
            self.drain()
        task = self.manager.status(self.task['id'])
        self.assertEqual(self.calls, [('neutral', 2, 2)])
        self.assertEqual([r['status'] for r in task['images']], ['idle', 'ready'])

    def test_clean_heading_names_and_duplicate_titles(self):
        headings = ['1. **数据体系结构** _⚠ 字号偏小_', '2. 数据体系结构',
                    '3. [API / 请求:流程](https://example.com)', '4. ' + '长标题' * 100]
        markdown = '\n'.join(f'## {heading}\n```mermaid\nflowchart LR\nA-->B\n```' for heading in headings)
        task = self.manager.preview('命名测试.md', markdown.encode('utf-8'))
        self.assertEqual(task['images'][0]['title'], '数据体系结构')
        self.assertEqual([r['name'] for r in task['images'][:3]],
                         ['01_数据体系结构.png', '02_数据体系结构.png', '03_API_请求_流程.png'])
        self.assertLess(len(task['images'][3]['name'].encode('utf-8')), 255)
        with patch.object(self.manager, '_render_png', side_effect=self.fake_render):
            self.manager.start_render(task['id'])
            self.drain()
        with zipfile.ZipFile(self.manager.archive_path(task['id'])) as archive:
            self.assertEqual(archive.namelist(), [r['name'] for r in task['images']])
        self.assertFalse((Path(self.temp.name) / task['id'] / 'export-r1').exists())

    def test_renderer_removes_source_and_theme_config_files(self):
        output = Path(self.temp.name) / self.task['id'] / 'images' / 'preview.png'
        self.manager.update_theme(self.task['id'], 'monochrome')
        snapshot = self.manager.tasks[self.task['id']]

        def render(command, **_kwargs):
            Path(command[command.index('-o') + 1]).write_bytes(b'png')
            return SimpleNamespace(returncode=0, stderr='', stdout='')

        with patch('app.mermaid_manager.shutil.which', return_value='mmdc'), \
             patch('app.mermaid_manager.subprocess.run', side_effect=render):
            self.manager._render_png(snapshot, snapshot['blocks'][0], output, 2)
        self.assertTrue(output.is_file())
        self.assertFalse(output.with_suffix('.mmd').exists())
        self.assertFalse(output.with_suffix('.json').exists())

    def test_stale_theme_render_cannot_replace_new_preview(self):
        started, release = threading.Event(), threading.Event()
        def slow(task, block, output, scale):
            started.set()
            release.wait(timeout=5)
            self.fake_render(task, block, output, scale)
        with patch.object(self.manager, '_render_png', side_effect=slow):
            self.manager.start_preview_render(self.task['id'], 1)
            self.assertTrue(started.wait(timeout=5))
            changed = self.manager.update_theme(self.task['id'], 'forest')
            release.set()
            self.drain()
        self.assertEqual(changed['revision'], 2)
        self.assertEqual(self.manager.status(self.task['id'])['images'][0]['status'], 'idle')
        with patch.object(self.manager, '_render_png', side_effect=self.fake_render):
            self.manager.start_preview_render(self.task['id'], 1)
            self.drain()
        self.assertIn('r2-preview-1', self.manager.status(self.task['id'])['images'][0]['url'])

    def test_export_all_with_current_theme_and_invalidate_archive(self):
        self.manager.update_theme(self.task['id'], 'custom', {'mainBkg':'#ff0000'})
        with patch.object(self.manager, '_render_png', side_effect=self.fake_render):
            running = self.manager.start_render(self.task['id'])
            self.assertEqual(running['status'], 'running')
            self.assertEqual(running['progress'], 1)
            self.drain()
        task = self.manager.status(self.task['id'])
        self.assertEqual(task['status'], 'completed')
        self.assertEqual(task['progress'], 100)
        with zipfile.ZipFile(self.manager.archive_path(self.task['id'])) as archive:
            self.assertEqual(archive.namelist(), ['01_First_chart.png', '02_Second_chart.png'])
            self.assertTrue(all(b'#ff0000' in archive.read(n) for n in archive.namelist()))
        self.assertEqual(self.calls, [('custom', 1, 3), ('custom', 2, 3)])
        self.manager.update_theme(self.task['id'], 'forest')
        self.assertIsNone(self.manager.archive_path(self.task['id']))
        with patch.object(self.manager, '_render_png', side_effect=self.fake_render):
            self.manager.start_render(self.task['id'])
            self.drain()
        with zipfile.ZipFile(self.manager.archive_path(self.task['id'])) as archive:
            self.assertTrue(all(archive.read(n) == b'forest:{}:3' for n in archive.namelist()))

    def test_monochrome_theme_uses_base_variables_and_white_background(self):
        self.manager.update_theme(self.task['id'], 'monochrome')
        captured = {}
        def capture(task, block, output, scale):
            captured.update(theme=task['theme'], variables=self.manager.MONOCHROME_VARIABLES, scale=scale)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b'png')
        with patch.object(self.manager, '_render_png', side_effect=capture):
            self.manager.start_preview_render(self.task['id'], 1)
            self.drain()
        self.assertEqual(captured['theme'], 'monochrome')
        self.assertEqual(captured['variables']['primaryColor'], '#ffffff')
        self.assertEqual(captured['variables']['lineColor'], '#111111')
        self.assertEqual(captured['scale'], 2)

    def test_failed_preview_retries_without_affecting_other_images(self):
        with patch.object(self.manager, '_render_png', side_effect=RuntimeError('invalid diagram')):
            self.manager.start_preview_render(self.task['id'], 1)
            self.drain()
        self.assertEqual(self.manager.status(self.task['id'])['images'][0]['status'], 'failed')
        with patch.object(self.manager, '_render_png', side_effect=self.fake_render):
            self.manager.start_preview_render(self.task['id'], 1)
            self.drain()
        self.assertEqual(self.manager.status(self.task['id'])['images'][0]['status'], 'ready')

    def test_delete_removes_task_and_generated_files(self):
        task_dir = Path(self.temp.name) / self.task['id']
        self.assertTrue(task_dir.is_dir())
        self.assertTrue(self.manager.delete(self.task['id']))
        self.assertIsNone(self.manager.status(self.task['id']))
        self.assertFalse(task_dir.exists())
        self.assertFalse(self.manager.delete(self.task['id']))

if __name__ == '__main__':
    unittest.main()
