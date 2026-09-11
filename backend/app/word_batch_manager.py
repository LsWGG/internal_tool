import io
import json
import re
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .word_batch_engine import (TOKEN, LIMIT, IMAGE_EXTENSIONS, checked_zip, normalized_image,
                                read_excel, render_document, safe_name, template_fields)


class WordBatchManager:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='word-batch')
        self.tasks = {}
        for file in self.root.glob('*/task.json'):
            try:
                task = json.loads(file.read_text('utf-8'))
                if task['id'] != file.parent.name:
                    continue
                if task['status'] in ('queued', 'running'):
                    task.update(status='failed', message='服务重启，任务已中断，可重新生成')
                self.tasks[task['id']] = task
                self._save(task)
            except (ValueError, KeyError):
                continue
        # Only expired unsubmitted workspaces are removed; completed tasks are retained.
        for folder in self.root.iterdir():
            if (folder.is_dir() and not folder.is_symlink() and re.fullmatch(r'[0-9a-f]{32}', folder.name)
                    and folder.name not in self.tasks and (folder / 'input.json').is_file()
                    and time.time() - (folder / 'input.json').stat().st_mtime > 86400):
                shutil.rmtree(folder)

    def path(self, ident):
        if not re.fullmatch(r'[0-9a-f]{32}', ident):
            raise ValueError('无效的任务标识')
        return self.root / ident

    def _save(self, task):
        target = self.path(task['id']) / 'task.json'
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(task, ensure_ascii=False), 'utf-8')
        temporary.replace(target)

    def _update(self, ident, **kwargs):
        with self.lock:
            self.tasks[ident].update(kwargs)
            self._save(self.tasks[ident])

    def prepare(self, template_name, template, excel_name, excel, files):
        if not template_name.lower().endswith('.docx') or not excel_name.lower().endswith('.xlsx'):
            raise ValueError('模板须为 .docx，Excel 须为 .xlsx')
        if not template or not excel or len(template) > 20 * 1024 * 1024 or len(excel) > 30 * 1024 * 1024:
            raise ValueError('模板不能为空且不超过 20MB，Excel 不超过 30MB')
        ident = uuid.uuid4().hex
        folder = self.path(ident)
        folder.mkdir()
        images = {}
        used_bytes = normalized_bytes = 0

        def add_image(name, content):
            nonlocal used_bytes, normalized_bytes
            name = name.replace('\\', '/').removeprefix('./')
            if name in images:
                raise ValueError(f'图片路径重复：{name}')
            used_bytes += len(content)
            if used_bytes > LIMIT or len(images) >= 1000:
                raise ValueError('图片总大小不能超过 200MB，最多 1000 张')
            data = normalized_image(content)
            normalized_bytes += len(data)
            if normalized_bytes > LIMIT:
                raise ValueError('图片解码后的总大小超过 200MB，请缩小图片或拆分批次')
            target = f'image_{len(images):04d}.png'
            (folder / target).write_bytes(data)
            images[name] = target

        try:
            fields = template_fields(template)
            for name, content in files:
                if name.lower().endswith('.zip'):
                    with checked_zip(content) as archive:
                        for item in archive.infolist():
                            if not item.is_dir() and not item.filename.startswith('__MACOSX/') and Path(item.filename).suffix.lower() in IMAGE_EXTENSIONS:
                                add_image(item.filename, archive.read(item))
                elif Path(name).suffix.lower() in IMAGE_EXTENSIONS:
                    add_image(name, content)
                else:
                    raise ValueError(f'不支持的图片文件：{name}')
            sheets = read_excel(excel, add_image)
            data = {'id': ident, 'template_name': Path(template_name).name, 'excel_name': Path(excel_name).name,
                    'fields': fields, 'sheets': sheets, 'images': images}
            (folder / 'template.docx').write_bytes(template)
            (folder / 'input.json').write_text(json.dumps(data, ensure_ascii=False), 'utf-8')
            return self.describe(ident)
        except Exception:
            shutil.rmtree(folder)
            raise

    def _input(self, ident):
        file = self.path(ident) / 'input.json'
        if not file.is_file():
            raise ValueError('上传内容不存在，请重新上传')
        return json.loads(file.read_text('utf-8'))

    def describe(self, ident):
        data = self._input(ident)
        return {**data, 'images': list(data['images']),
                'sheets': [{**sheet, 'count': len(sheet['rows']), 'rows': sheet['rows'][:3]} for sheet in data['sheets']]}

    def config(self, ident, config):
        data = self._input(ident)
        sheet = next((s for s in data['sheets'] if s['name'] == config.get('sheet')), None)
        if sheet is None:
            raise ValueError('请选择有效工作表')
        mappings = config.get('mappings', {})
        if not isinstance(mappings, dict):
            raise ValueError('字段映射格式不正确')
        normalized = {}
        for field in data['fields']:
            mapping = mappings.get(field['key'], {})
            if not isinstance(mapping, dict) or mapping.get('column') not in sheet['columns']:
                raise ValueError(f'请为“{field["key"]}”选择 Excel 列')
            kind = mapping.get('type', 'text')
            if kind not in ('text', 'image') or (field['image'] and kind != 'image'):
                raise ValueError(f'“{field["key"]}”的字段类型不正确')
            width, height = float(mapping.get('width_mm', 40)), float(mapping.get('height_mm', 40))
            if not 1 <= width <= 170 or not 1 <= height <= 240:
                raise ValueError('图片最大宽度为 170mm、最大高度为 240mm，不能小于 1mm')
            normalized[field['key']] = {'column': mapping['column'], 'type': kind, 'width_mm': width, 'height_mm': height}
        naming = str(config.get('filename', '')).strip()[:200]
        if any(m.group(1).strip() not in sheet['columns'] + ['序号'] for m in TOKEN.finditer(naming)):
            raise ValueError('文件名模板只能使用 Excel 列名及 {{序号}}')
        return data, sheet, {'sheet': sheet['name'], 'mappings': normalized, 'filename': naming}

    def _image(self, folder, data, value):
        value = value.replace('\\', '/').removeprefix('./').strip()
        target = data['images'].get(value)
        if target is None:
            matches = [file for name, file in data['images'].items() if Path(name).name == value]
            if len(matches) == 1:
                target = matches[0]
            elif len(matches) > 1:
                raise ValueError(f'图片重名，请在 Excel 填写完整相对路径：{value}')
        if target is None:
            raise ValueError(f'未找到图片“{value}”；请上传对应图片，不支持本机绝对路径或网络地址')
        if not re.fullmatch(r'image_\d+\.png', target):
            raise ValueError('图片索引无效')
        return (folder / target).read_bytes()

    def preview(self, ident, config):
        if ident in self.tasks:
            raise ValueError('请使用任务结果预览入口')
        data, sheet, config = self.config(ident, config)
        # A unique immutable file avoids one tab replacing another tab's preview.
        name = 'preview_' + uuid.uuid4().hex + '.docx'
        folder = self.path(ident)
        result = render_document((folder / 'template.docx').read_bytes(), sheet['rows'][0]['values'], config['mappings'],
                                 lambda value: self._image(folder, data, value))
        (folder / name).write_bytes(result)
        return name

    def create(self, ident, config):
        _, sheet, normalized = self.config(ident, config)
        task_id = uuid.uuid4().hex
        folder = self.path(task_id)
        with self.lock:
            if ident in self.tasks:
                raise ValueError('不能将结果任务用作上传工作区')
            shutil.copytree(self.path(ident), folder, ignore=shutil.ignore_patterns('preview_*.docx'))
            task = {'id': task_id, 'name': f'{Path(self._input(ident)["template_name"]).stem} · {len(sheet["rows"])} 份',
                    'status': 'queued', 'total': len(sheet['rows']), 'processed': 0, 'success': 0, 'failed': 0,
                    'message': '等待生成', 'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'config': normalized, 'results': []}
            self.tasks[task_id] = task
            self._save(task)
        self.pool.submit(self._run, task_id)
        return self.summary(task)

    def _run(self, ident):
        try:
            data, sheet, config = self.config(ident, self.tasks[ident]['config'])
            folder = self.path(ident)
            output = folder / 'results'
            if output.exists():
                shutil.rmtree(output)
            output.mkdir()
            template = (folder / 'template.docx').read_bytes()
            results, names, success = [], set(), 0
            generated_bytes = 0
            self._update(ident, status='running', message='正在生成', results=[], success=0, failed=0, processed=0)
            for index, row in enumerate(sheet['rows'], 1):
                result = {'index': index, 'row': row['row']}
                try:
                    filename = TOKEN.sub(lambda m: str(index) if m.group(1).strip() == '序号' else str(row['values'].get(m.group(1).strip(), '')), config['filename']) if config['filename'] else row['values'].get(sheet['columns'][0], '')
                    stem = safe_name(filename or f'{Path(data["template_name"]).stem}_{index}')
                    if stem.lower().endswith('.docx'):
                        stem = stem[:-5]
                    name, suffix = stem + '.docx', 2
                    while name.casefold() in names:
                        name = f'{stem}_{suffix}.docx'
                        suffix += 1
                    names.add(name.casefold())
                    document = render_document(template, row['values'], config['mappings'], lambda value: self._image(folder, data, value))
                    if generated_bytes + len(document) > 1024 * 1024 * 1024:
                        raise ValueError('该任务生成结果已达到 1GB，请拆分 Excel 后继续')
                    generated_bytes += len(document)
                    (output / name).write_bytes(document)
                    result.update(name=name, status='completed')
                    success += 1
                except Exception as exc:
                    result.update(status='failed', error=str(exc))
                results.append(result)
                self._update(ident, processed=index, success=success, failed=index-success, results=results[:], message=f'已处理 {index}/{len(sheet["rows"])} 行')
            if success:
                with zipfile.ZipFile(folder / 'results.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
                    for result in results:
                        if result['status'] == 'completed':
                            archive.write(output / result['name'], result['name'])
                    if success < len(results):
                        archive.writestr('失败明细.json', json.dumps([r for r in results if r['status'] == 'failed'], ensure_ascii=False, indent=2))
            self._update(ident, status='completed' if success == len(results) else 'partial' if success else 'failed',
                         message=f'生成完成：成功 {success} 份，失败 {len(results)-success} 份')
        except Exception as exc:
            self._update(ident, status='failed', message=str(exc))

    @staticmethod
    def summary(task):
        return {key: value for key, value in task.items() if key not in ('results', 'config')}

    def list_tasks(self):
        with self.lock:
            return [self.summary(task) for task in reversed(list(self.tasks.values()))]

    def results(self, ident, page=1):
        with self.lock:
            task = self.tasks.get(ident)
            if task is None:
                raise ValueError('任务不存在')
            return {'items': task['results'][(page-1)*20:page*20], 'total': len(task['results']), 'page': page}

    def result_file(self, ident, index=None):
        with self.lock:
            task = self.tasks.get(ident)
            if not task:
                raise ValueError('任务不存在')
            if index is None:
                path = self.path(ident) / 'results.zip'
                if task['status'] in ('queued', 'running'):
                    raise ValueError('任务尚未结束，请等待后再下载 ZIP')
            else:
                result = next((r for r in task['results'] if r['index'] == index and r['status'] == 'completed'), None)
                if not result:
                    raise ValueError('结果不存在或生成失败')
                path = self.path(ident) / 'results' / result['name']
            if not path.is_file():
                raise ValueError('结果文件不存在')
            return path

    def delete(self, ident, workspace=False):
        with self.lock:
            task = self.tasks.get(ident)
            if workspace and task:
                raise ValueError('该标识属于任务，请使用任务删除入口')
            if task and task['status'] in ('queued', 'running'):
                raise ValueError('任务仍在生成，请完成后再删除')
            folder = self.path(ident)
            if folder.exists():
                shutil.rmtree(folder)
            self.tasks.pop(ident, None)

    def retry(self, ident):
        with self.lock:
            task = self.tasks.get(ident)
            if not task or task['status'] not in ('failed', 'partial'):
                raise ValueError('仅失败或部分失败的任务可重新生成')
            task.update(status='queued', message='等待重新生成', results=[], success=0, failed=0, processed=0)
            self._save(task)
        self.pool.submit(self._run, ident)
        return self.summary(task)
