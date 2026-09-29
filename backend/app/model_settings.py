"""Live model settings. Secrets stay in an ignored, owner-readable file."""
import json
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

ROOT=Path(__file__).resolve().parents[1]/'model_settings_data'
LOCK=threading.RLock()
SCOPES=('assistant','ocr')


def read():
    path=ROOT/'settings.json'
    return json.loads(path.read_text()) if path.exists() else {}


def resolve(scope):
    with LOCK:
        saved=read().get(scope)
    if saved is not None:
        return saved['url'],saved.get('api_key',''),saved['model']
    prefixes=('PDF_OCR_','AI_NATIVE_LLM_','CRAWLER_LLM_') if scope=='ocr' else ('AI_NATIVE_LLM_','CRAWLER_LLM_')
    for prefix in prefixes:
        values=tuple(os.getenv(prefix+key,'').strip() for key in ('URL','API_KEY','MODEL'))
        if any(values): return values
    return '','',''


class Settings(BaseModel):
    model_config=ConfigDict(extra='forbid')
    url:str=Field(max_length=2048)
    model:str=Field(max_length=200)
    api_key:str=Field(default='',max_length=4096)
    clear_key:bool=False


def candidate(scope, payload):
    url=payload.url.strip(); model=payload.model.strip()
    parsed=urlsplit(url)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('请输入不含密钥、查询参数的完整 HTTP(S) chat/completions 地址')
    if not parsed.path.rstrip('/').endswith('/chat/completions'):
        raise ValueError('请填写完整接口地址，以 /chat/completions 结尾')
    if not model: raise ValueError('请填写模型名称')
    old_url,old_key,_=resolve(scope)
    # A changed destination never inherits a credential silently.
    key='' if payload.clear_key else payload.api_key.strip() or (old_key if old_url==url else '')
    return {'url':url,'model':model,'api_key':key}


def public():
    saved=read()
    result={}
    for scope in SCOPES:
        url,key,model=resolve(scope)
        result[scope]={'url':url,'model':model,'key_set':bool(key),'source':'settings' if scope in saved else 'environment'}
    return result


def make_router():
    router=APIRouter(prefix='/api/settings/models',tags=['大模型配置'])
    @router.get('')
    def get():
        with LOCK: return public()
    @router.put('/{scope}')
    def save(scope:str,payload:Settings):
        if scope not in SCOPES: raise HTTPException(404,'配置用途不存在')
        with LOCK:
            try: value=candidate(scope,payload)
            except ValueError as exc: raise HTTPException(400,str(exc)) from exc
            values=read();values[scope]=value
            ROOT.mkdir(parents=True,exist_ok=True)
            path=ROOT/'settings.json';temp=ROOT/'settings.tmp'
            fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as stream: json.dump(values,stream,ensure_ascii=False,indent=2)
            os.chmod(temp,0o600);os.replace(temp,path)
            return public()
    @router.post('/{scope}/test')
    def test(scope:str,payload:Settings):
        if scope not in SCOPES: raise HTTPException(404,'配置用途不存在')
        try:
            value=candidate(scope,payload)
            if scope=='ocr':
                import base64,io
                from PIL import Image,ImageDraw
                from .pdf_toolbox import request_ocr
                image=Image.new('RGB',(320,100),'white');ImageDraw.Draw(image).text((30,30),'TEST 123',fill='black')
                data=io.BytesIO();image.save(data,format='PNG')
                request_ocr(value['url'],value['api_key'],value['model'],base64.b64encode(data.getvalue()).decode())
                return {'message':'图片请求测试通过，模型返回了非空内容。'}
            response=requests.post(value['url'],headers={'Authorization':'Bearer '+value['api_key']},json={'model':value['model'],'messages':[{'role':'user','content':'Reply OK.'}],'max_tokens':32},timeout=(10,30))
            response.raise_for_status()
            content=response.json().get('choices',[{}])[0].get('message',{}).get('content')
            if not content: raise ValueError('模型没有返回文本内容')
            return {'message':'连接测试通过，模型返回了文本内容。'}
        except ValueError as exc: raise HTTPException(400,str(exc)) from exc
        except requests.RequestException as exc:
            status=exc.response.status_code if exc.response is not None else None
            raise HTTPException(400,f'连接测试失败：HTTP {status}，请检查地址、模型权限和密钥。' if status else '模型连接失败或超时，请检查网络和接口地址。') from exc
        except (KeyError,IndexError,TypeError): raise HTTPException(400,'模型返回格式不兼容')
    return router
