import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch, Mock
import pymupdf as fitz
from docx import Document
from PIL import Image
from app.pdf_toolbox import process, pages_for

class PdfToolboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.folder=Path(self.tmp.name)
        self.source=self.folder/'sample.pdf'
        with fitz.open() as doc:
            for i in range(3): doc.new_page().insert_text((72,72),f'Toolbox page {i+1}')
            doc.save(self.source)
    def tearDown(self): self.tmp.cleanup()
    def run_op(self,op,options=None,paths=None):
        return process(self.folder,paths or [self.source],op,options or {})
    def test_pages_validation(self):
        self.assertEqual(pages_for('3-1,2',3),[2,1,0,1])
        for value in ['0','4','abc','1,']:
            with self.assertRaises(ValueError): pages_for(value,3)
    def test_page_operations(self):
        for op,opts,count,text in [('merge',{},6,'page 1'),('extract',{'pages':'3,1'},2,'page 3'),('delete',{'pages':'2'},2,'page 1'),('compress',{},3,'page 1'),('watermark',{'watermark':'内部资料'},3,'page 1')]:
            with self.subTest(op=op):
                result=self.run_op(op,opts,[self.source,self.source] if op=='merge' else None)
                with fitz.open(result) as doc:
                    self.assertEqual(len(doc),count);self.assertIn(text,doc[0].get_text())
        with self.assertRaises(ValueError): self.run_op('delete',{'pages':'1-3'})
    def test_rotation_preserves_unselected(self):
        result=self.run_op('rotate',{'pages':'2','angle':90})
        with fitz.open(result) as doc:self.assertEqual([p.rotation for p in doc],[0,90,0])
    def test_word_is_real_docx(self):
        result=self.run_op('pdf-to-word',{'pages':'1-2'})
        doc=Document(result)
        text=' '.join(p.text for p in doc.paragraphs)
        self.assertIn('Toolbox page 1',text);self.assertIn('Toolbox page 2',text)
        self.assertNotIn('Toolbox page 3',text)
    def test_exports_and_image_input(self):
        for op,suffix in [('split','.pdf'),('pdf-to-images','.png')]:
            result=self.run_op(op,{'pages':'2,3'})
            with zipfile.ZipFile(result) as archive:
                self.assertEqual(len(archive.namelist()),2)
                self.assertTrue(all(n.endswith(suffix) for n in archive.namelist()))
        self.assertIn('Toolbox page 2',self.run_op('text').read_text())
        image=self.folder/'image.png';Image.new('RGB',(200,100),'cyan').save(image)
        with fitz.open(self.run_op('images-to-pdf',paths=[image])) as doc:self.assertEqual(len(doc),1)
    def test_ocr_multimodal_contract(self):
        response=Mock();response.json.return_value={'choices':[{'message':{'content':'识别文本'}}]}
        with patch('app.pdf_toolbox.model_config',return_value=('https://example.invalid/chat/completions','secret','deepseek-vision')),patch('app.pdf_toolbox.requests.post',return_value=response) as request:
            result=self.run_op('ocr',{'pages':'1'})
            self.assertIn('识别文本',result.read_text())
            self.assertEqual(request.call_args.kwargs['json']['messages'][0]['content'][1]['type'],'image_url')

class PdfToolboxApiTests(PdfToolboxTests):
    def test_http_conversion_and_bad_input(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.pdf_toolbox import make_router
        app=FastAPI();app.include_router(make_router(self.enterContext(tempfile.TemporaryDirectory())))
        with TestClient(app) as client:
            response=client.post('/api/pdf-toolbox/process',data={'operation':'pdf-to-word','options':'{"pages":"1"}'},files={'files':('sample.pdf',self.source.read_bytes(),'application/pdf')})
            self.assertEqual(response.status_code,200,response.text[:300] if response.status_code!=200 else '')
            import io
            self.assertTrue(zipfile.is_zipfile(io.BytesIO(response.content)))
            self.assertIn('.docx',response.headers['content-disposition'])
            bad=client.post('/api/pdf-toolbox/process',data={'operation':'delete','options':'{"pages":"99"}'},files={'files':('sample.pdf',self.source.read_bytes(),'application/pdf')})
            self.assertEqual(bad.status_code,400)
            self.assertIn('页码',bad.json()['detail'])
            self.assertEqual(client.get('/api/pdf-toolbox/capabilities').status_code,200)

class OcrDiagnosticsTests(unittest.TestCase):
    def test_config_does_not_mix_providers(self):
        from app.pdf_toolbox import model_config, ocr_configuration_error
        with patch.dict('os.environ',{'PDF_OCR_MODEL':'vision','CRAWLER_LLM_URL':'https://other.invalid','CRAWLER_LLM_API_KEY':'other-key'},clear=True):
            self.assertEqual(model_config(),('','','vision'))
        self.assertIn('文本模型',ocr_configuration_error('https://example.invalid','glm-4-flash'))
    def test_http_and_timeout_errors_are_actionable_and_redacted(self):
        import requests
        from app.pdf_toolbox import request_ocr
        for status,expected in [(401,'鉴权'),(429,'限流'),(400,'图片'),(503,'暂时不可用')]:
            response=Mock(status_code=status)
            response.raise_for_status.side_effect=requests.HTTPError('SECRET request body')
            with patch('app.pdf_toolbox.requests.post',return_value=response):
                with self.assertRaises(ValueError) as caught: request_ocr('url','key','vision','image')
                self.assertIn(expected,str(caught.exception));self.assertNotIn('SECRET',str(caught.exception))
        with patch('app.pdf_toolbox.requests.post',side_effect=requests.Timeout()):
            with self.assertRaisesRegex(ValueError,'超时'):request_ocr('url','key','vision','image')

class ProgressTests(unittest.TestCase):
    def test_page_and_api_progress(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.pdf_toolbox import make_router
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);source=folder/'sample.pdf'
            with fitz.open() as doc:
                for _ in range(3): doc.new_page()
                doc.save(source)
            updates=[]
            process(folder,[source],'split',{},lambda value,label:updates.append((value,label)))
            values=[value for value,_ in updates]
            self.assertEqual(values,sorted(values))
            self.assertEqual(values[-1],90)
            self.assertTrue(any('2/3' in label for _,label in updates))
            app=FastAPI();app.include_router(make_router(self.enterContext(tempfile.TemporaryDirectory())))
            with TestClient(app) as client:
                task='test-progress-123456'
                response=client.post('/api/pdf-toolbox/process',data={'operation':'split','task_id':task},files={'files':('sample.pdf',source.read_bytes(),'application/pdf')})
                self.assertEqual(response.status_code,200)
                state=client.get('/api/pdf-toolbox/progress/'+task).json()
                self.assertEqual(state['progress'],95)
                self.assertNotIn('updated',state)
                self.assertEqual(client.get('/api/pdf-toolbox/progress/missing').status_code,404)

class WatermarkTests(unittest.TestCase):
    def test_visible_on_rotated_and_cropped_pages(self):
        from app.pdf_toolbox import add_watermark
        with fitz.open() as doc:
            for rotation in (0,90,180,270):
                page=doc.new_page(width=595,height=842)
                page.set_cropbox(fitz.Rect(30,40,560,800))
                page.set_rotation(rotation)
                add_watermark(page,'内部资料')
                self.assertEqual(page.rotation,rotation)
                self.assertIn('内部资料',page.get_text().replace(' ',''))
                pix=page.get_pixmap()
                pixels=zip(*[iter(pix.samples)]*pix.n)
                red=sum(1 for pixel in pixels if pixel[0]>pixel[1]+20 and pixel[0]>pixel[2]+20)
                self.assertGreater(red,500)
    def test_blank_watermark_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);source=folder/'input.pdf'
            with fitz.open() as doc:
                doc.new_page();doc.save(source)
            with self.assertRaisesRegex(ValueError,'水印文字'):
                process(folder,[source],'watermark',{'watermark':'  '})

class WatermarkOverlayTests(unittest.TestCase):
    def test_watermark_is_not_hidden_by_original_page_clip(self):
        from app.pdf_toolbox import add_watermark
        with fitz.open() as doc:
            page=doc.new_page()
            page.insert_text((40,40),'Clipped original')
            xref=page.get_contents()[0]
            doc.update_stream(xref,doc.xref_stream(xref)+b'\n0 0 1 1 re W n\n')
            add_watermark(page,'内部资料')
            with fitz.open(stream=doc.tobytes(),filetype='pdf') as result:
                pix=result[0].get_pixmap()
                red=sum(1 for r,g,b in zip(*[iter(pix.samples)]*3) if r>g+20 and r>b+20)
                self.assertGreater(red,500)

class PreviewTests(unittest.TestCase):
    def test_preview_renders_watermark_and_rejects_invalid_page(self):
        import io
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.pdf_toolbox import make_router, add_watermark
        app=FastAPI();app.include_router(make_router(self.enterContext(tempfile.TemporaryDirectory())))
        with fitz.open() as doc:
            page=doc.new_page();add_watermark(page,'内部资料');data=doc.tobytes()
        with TestClient(app) as client:
            response=client.post('/api/pdf-toolbox/preview',files={'file':('result.pdf',data)},data={'page':1})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers['x-pdf-pages'],'1')
            self.assertEqual(response.headers['cache-control'],'no-store')
            image=Image.open(io.BytesIO(response.content)).convert('RGB')
            self.assertGreater(sum(r>g+20 and r>b+20 for r,g,b in image.getdata()),500)
            self.assertEqual(client.post('/api/pdf-toolbox/preview',files={'file':('result.pdf',data)},data={'page':2}).status_code,400)
            self.assertEqual(client.post('/api/pdf-toolbox/preview',files={'file':('result.pdf',b'broken')}).status_code,400)

class ResultPersistenceTests(unittest.TestCase):
    def test_restart_download_and_removal(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.pdf_toolbox import make_router
        with tempfile.TemporaryDirectory() as directory:
            def client():
                app=FastAPI();app.include_router(make_router(directory));return TestClient(app)
            with fitz.open() as doc:
                doc.new_page();source=doc.tobytes()
            with client() as first:
                response=first.post('/api/pdf-toolbox/process',data={'operation':'watermark','options':'{"watermark":"内部资料"}'},files={'files':('sample.pdf',source)})
                self.assertEqual(response.status_code,200)
                result_id=response.headers['x-result-id'];output=response.content
            with client() as restarted:
                records=restarted.get('/api/pdf-toolbox/results').json()
                self.assertEqual(len(records),1)
                self.assertEqual(records[0]['id'],result_id)
                self.assertEqual(records[0]['name'],'sample-watermark.pdf')
                self.assertEqual(restarted.get(f'/api/pdf-toolbox/results/{result_id}/download').content,output)
                self.assertEqual(restarted.get('/api/pdf-toolbox/results/invalid/download').status_code,404)
                self.assertEqual(restarted.delete(f'/api/pdf-toolbox/results/{result_id}').status_code,200)
                self.assertEqual(restarted.get('/api/pdf-toolbox/results').json(),[])
                self.assertEqual(restarted.get(f'/api/pdf-toolbox/results/{result_id}/download').status_code,404)
