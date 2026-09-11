import json, shutil, threading, uuid, re, time
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import pandas as pd
from PIL import Image
from playwright.sync_api import sync_playwright

DESKTOP_VIEWPORT_WIDTH = 1920
DESKTOP_VIEWPORT_HEIGHT = 1080
DESKTOP_DEVICE_SCALE_FACTOR = 2
# 1920 CSS px 配合 2x DPR 对应 3840x2160 的 4K 桌面显示。
# 应用型网页不能使用 print 流式排版，否则固定侧栏、画布和主工作区会被
# 浏览器拆到不同纸页。先按屏幕媒体完整截图，再将截图纵向切片放入 A4 横版。
A4_LANDSCAPE_SIZE = (3508, 2480)  # 300 DPI
A4_MARGIN_PX = 118  # 约 10mm @ 300 DPI


def save_desktop_snapshot_pdf(page, path):
    """将当前桌面网页按视觉结果写入标准 A4 横向 PDF。"""
    screenshot = page.screenshot(full_page=True, type='png', animations='disabled')
    source = Image.open(BytesIO(screenshot))
    if source.mode != 'RGB':
        background = Image.new('RGB', source.size, 'white')
        if source.mode == 'RGBA':
            background.paste(source, mask=source.getchannel('A'))
        else:
            background.paste(source.convert('RGB'))
        source = background

    page_width, page_height = A4_LANDSCAPE_SIZE
    content_width = page_width - A4_MARGIN_PX * 2
    content_height = page_height - A4_MARGIN_PX * 2
    scale = content_width / source.width
    source_slice_height = max(1, int(content_height / scale))
    pages = []

    for top in range(0, source.height, source_slice_height):
        bottom = min(source.height, top + source_slice_height)
        part = source.crop((0, top, source.width, bottom))
        target_height = max(1, round(part.height * scale))
        part = part.resize((content_width, target_height), Image.Resampling.LANCZOS)
        sheet = Image.new('RGB', A4_LANDSCAPE_SIZE, 'white')
        # 单屏应用通常是 16:9，而 A4 横版稍高；单页时上下居中，避免所有
        # 留白都堆在页面底部。多页内容仍从页顶连续排列。
        target_y = A4_MARGIN_PX
        if source.height <= source_slice_height:
            target_y += (content_height - target_height) // 2
        sheet.paste(part, (A4_MARGIN_PX, target_y))
        pages.append(sheet)

    if not pages:
        pages = [Image.new('RGB', A4_LANDSCAPE_SIZE, 'white')]
    pages[0].save(
        path,
        'PDF',
        resolution=300,
        quality=92,
        optimize=True,
        save_all=True,
        append_images=pages[1:],
    )

def normalize_url(value):
    value=str(value or '').strip()
    if not value or value.lower()=='nan': return ''
    if value.startswith('['):
        try: value=str(json.loads(value)[0])
        except Exception: pass
    if not re.match(r'^https?://', value, re.I): value='https://'+value
    return value

def render_filename(template, index, url, title):
    host=(urlparse(url).hostname or 'webpage').removeprefix('www.')
    values={'index':f'{index:03d}','host':host,'title':title.strip() or host}
    name=str(template or '{index}_{host}')
    for key,value in values.items(): name=name.replace('{'+key+'}',value)
    name=re.sub(r'[\\/:*?"<>|\x00-\x1f]+','_',name)
    name=re.sub(r'\s+',' ',name).strip(' ._')[:140]
    return name or f'{index:03d}_{host}'

class PDFTaskManager:
    def __init__(self,data_dir):
        self.data_dir=Path(data_dir);self.data_dir.mkdir(parents=True,exist_ok=True);self.state_file=self.data_dir/'tasks.json';self.lock=threading.RLock();self.pool=ThreadPoolExecutor(max_workers=2)
        try:self.tasks=json.loads(self.state_file.read_text())
        except Exception:self.tasks={}
    def _save(self):
        self.data_dir.mkdir(parents=True,exist_ok=True);tmp=self.state_file.with_suffix('.tmp');tmp.write_text(json.dumps(self.tasks,ensure_ascii=False,indent=2));tmp.replace(self.state_file)
    def _update(self,i,**v):
        with self.lock:
            if i in self.tasks:self.tasks[i].update(v,updated_at=datetime.now(timezone.utc).isoformat());self._save()
    def create(self,urls,filename_template='{index}_{host}'):
        urls=[normalize_url(x) for x in urls];urls=[x for x in urls if x]
        if not urls: raise ValueError('没有解析到有效网址')
        filename_template=str(filename_template or '{index}_{host}').strip()
        if len(filename_template)>160: raise ValueError('文件名模板不能超过 160 个字符')
        i=uuid.uuid4().hex;now=datetime.now(timezone.utc).isoformat();self.tasks[i]={'id':i,'name':f'网页转PDF · {len(urls)}个网址','status':'queued','progress':0,'current':0,'total':len(urls),'message':'等待执行','created_at':now,'updated_at':now,'urls':urls,'filename_template':filename_template,'result':None,'error':None};self._save();self.pool.submit(self._run,i);return self.tasks[i]
    def _run(self,i):
        task=self.tasks[i];out=self.data_dir/i;out.mkdir(parents=True,exist_ok=True);files=[]
        try:
            self._update(i,status='running',message='启动浏览器')
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True,args=['--disable-blink-features=AutomationControlled']);page=browser.new_page(viewport={'width':DESKTOP_VIEWPORT_WIDTH,'height':DESKTOP_VIEWPORT_HEIGHT},device_scale_factor=DESKTOP_DEVICE_SCALE_FACTOR,user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36',locale='zh-CN',ignore_https_errors=True)
                page.emulate_media(media='screen')
                for n,url in enumerate(task['urls'],1):
                    self._update(i,current=n-1,progress=round((n-1)/len(task['urls'])*90,1),message=f'正在转换 {n}/{len(task["urls"])}：{url}')
                    # Some government/news sites keep analytics connections open forever;
                    # waiting for networkidle would therefore fail despite a usable page.
                    try: page.goto(url,wait_until='domcontentloaded',timeout=90000)
                    except Exception as nav_error:
                        # A timeout can still leave a fully rendered document; only abort
                        # when the page has no body/content at all.
                        if not page.locator('body').count(): raise nav_error
                    page.wait_for_timeout(1800)
                    # Trigger image lazy-loaders used by shopping/news sites. Copy the
                    # common lazy source attributes, then scroll through a bounded part
                    # of the document so IntersectionObserver callbacks can run.
                    page.evaluate("""async () => {
                      if (document.fonts?.ready) await document.fonts.ready;
                      document.querySelectorAll('img').forEach(img => {
                        const lazy = img.dataset.src || img.dataset.original || img.dataset.lazySrc || img.getAttribute('data-ks-lazyload');
                        if (lazy && (!img.src || img.src.startsWith('data:'))) img.src = lazy;
                        img.loading = 'eager';
                      });
                      const height = Math.min(document.documentElement.scrollHeight, window.innerHeight * 12);
                      for (let y = 0; y < height; y += Math.max(500, window.innerHeight * .8)) {
                        window.scrollTo(0, y);
                        await new Promise(resolve => setTimeout(resolve, 280));
                      }
                      window.scrollTo(0, 0);
                      await new Promise(resolve => setTimeout(resolve, 700));
                      const images = [...document.images].filter(img => img.src);
                      await Promise.race([
                        Promise.allSettled(images.map(img => img.complete ? img.decode?.().catch(()=>{}) : new Promise(resolve => { img.addEventListener('load', resolve, {once:true}); img.addEventListener('error', resolve, {once:true}); }))),
                        new Promise(resolve => setTimeout(resolve, 12000))
                      ]);
                    }""")
                    base=render_filename(task.get('filename_template','{index}_{host}'),n,url,page.title())
                    path=out/f'{base}.pdf';suffix=2
                    while path.exists(): path=out/f'{base}_{suffix}.pdf';suffix+=1
                    save_desktop_snapshot_pdf(page, path)
                    files.append({'name':path.name,'url':url,'size':path.stat().st_size})
                browser.close()
            archive=shutil.make_archive(str(out),'zip',out);self._update(i,status='completed',progress=100,current=len(task['urls']),message='转换完成',result={'files':files,'archive':Path(archive).name})
        except Exception as exc:self._update(i,status='failed',message='转换失败',error=str(exc))
    def list(self):
        with self.lock:return sorted(self.tasks.values(),key=lambda x:x['created_at'],reverse=True)
    def get(self,i):return self.tasks.get(i)
    def retry(self,i):
        with self.lock:
            task=self.tasks.get(i)
            if not task: return None
            if task.get('status')!='failed': raise ValueError('只有失败任务可以重新转换')
            task.update(status='queued',progress=0,current=0,message='等待重新转换',result=None,error=None,updated_at=datetime.now(timezone.utc).isoformat())
            self._save()
        shutil.rmtree(self.data_dir/i,ignore_errors=True)
        (self.data_dir/f'{i}.zip').unlink(missing_ok=True)
        self.pool.submit(self._run,i)
        return task
    def delete(self,i):
        with self.lock:
            if not self.tasks.pop(i,None): return False
            self._save()
        shutil.rmtree(self.data_dir/i,ignore_errors=True);(self.data_dir/f'{i}.zip').unlink(missing_ok=True);return True

    def create_from_excel(self,path,url_column=None,filename_template='{index}_{host}'):
        frame=pd.read_excel(path)
        if frame.empty: raise ValueError('Excel 没有数据')
        column=url_column if url_column in frame.columns else frame.columns[0]
        return self.create(frame[column].tolist(),filename_template)
