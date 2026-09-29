import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import model_settings as models

class ModelSettingsTests(unittest.TestCase):
    def test_save_redaction_live_resolution_and_key_destination(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(models,'ROOT',Path(folder)),patch.dict('os.environ',{'CRAWLER_LLM_URL':'https://old.example/chat/completions','CRAWLER_LLM_MODEL':'old','CRAWLER_LLM_API_KEY':'environment-secret'},clear=True):
            app=FastAPI();app.include_router(models.make_router())
            with TestClient(app) as client:
                before=client.get('/api/settings/models').json()
                self.assertTrue(before['assistant']['key_set']);self.assertNotIn('environment-secret',str(before))
                payload={'url':'https://new.example/v1/chat/completions','model':'vision','api_key':'new-secret'}
                response=client.put('/api/settings/models/ocr',json=payload)
                self.assertEqual(response.status_code,200);self.assertNotIn('new-secret',response.text)
                self.assertEqual(models.resolve('ocr'),(payload['url'],'new-secret','vision'))
                payload['api_key']='';client.put('/api/settings/models/ocr',json=payload)
                self.assertEqual(models.resolve('ocr')[1],'new-secret')
                payload['url']='https://another.example/chat/completions';client.put('/api/settings/models/ocr',json=payload)
                self.assertEqual(models.resolve('ocr')[1],'')
                self.assertEqual((Path(folder)/'settings.json').stat().st_mode & 0o777,0o600)
                self.assertEqual(models.resolve('assistant')[2],'old')
                payload['url']='https://example.com/?key=bad'
                self.assertEqual(client.put('/api/settings/models/ocr',json=payload).status_code,400)
