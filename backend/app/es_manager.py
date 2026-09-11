import json
import shutil
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from .models import ESConnection, ESExportTask
from .task_manager import TaskCancelled, TaskControl


class ESClient:
    def __init__(self, connection: ESConnection):
        self.base = connection.host.rstrip("/")
        self.session = requests.Session()
        if connection.username: self.session.auth = (connection.username, connection.password)
        self.verify = connection.verify_certs

    def request(self, method, path, **kwargs):
        response = self.session.request(method, self.base + path, timeout=90, verify=self.verify, **kwargs)
        if not response.ok:
            raise RuntimeError(f"ES {response.status_code}: {response.text[:500]}")
        return response.json() if response.content else {}

    def info(self): return self.request("GET", "/")
    def indices(self): return self.request("GET", "/_cat/indices?format=json&h=index,docs.count,store.size,status&expand_wildcards=open")


class ESTaskManager:
    def __init__(self, data_dir: Path):
        self.data_dir=data_dir;data_dir.mkdir(parents=True,exist_ok=True);self.state_file=data_dir/"tasks.json"
        self.lock=threading.RLock();self.controls={};self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix="es-task")
        try:self.tasks=json.loads(self.state_file.read_text("utf-8"))
        except (FileNotFoundError,json.JSONDecodeError):self.tasks={}
        for task in self.tasks.values():
            if task["status"] in ("queued","running","paused"):task.update(status="failed",message="服务重启，任务已中断")
        self._save()

    def _save(self):
        self.data_dir.mkdir(parents=True,exist_ok=True)
        temp=self.state_file.with_suffix(".tmp");temp.write_text(json.dumps(self.tasks,ensure_ascii=False,indent=2),"utf-8");temp.replace(self.state_file)
    def _update(self,task_id,**values):
        with self.lock:
            if task_id not in self.tasks:return
            values["updated_at"]=datetime.now(timezone.utc).isoformat();self.tasks[task_id].update(values);self._save()
    def list(self):
        with self.lock:return sorted(self.tasks.values(),key=lambda x:x["created_at"],reverse=True)
    def get(self,task_id):
        with self.lock:return self.tasks.get(task_id)

    def _create_record(self,mode,connection,name,details):
        task_id,now=uuid.uuid4().hex,datetime.now(timezone.utc).isoformat()
        record={"id":task_id,"mode":mode,"name":name,"status":"queued","progress":0,"message":"等待执行",
                "created_at":now,"updated_at":now,"request":{"host":connection.host,"username":connection.username,
                "verify_certs":connection.verify_certs,**details},"result":None,"error":None}
        with self.lock:self.tasks[task_id]=record;self.controls[task_id]=TaskControl();self._save()
        return task_id,record

    def create_export(self,request:ESExportTask):
        task_id,record=self._create_record("export",request.connection,"ES导出_"+"_".join(request.indexes[:3]),
                                           {"indexes":request.indexes})
        self.pool.submit(self._run_export,task_id,request);return record

    def _run_export(self,task_id,request):
        control=self.controls[task_id];task_dir=self.data_dir/task_id;output=task_dir/"output";(output/"mapping").mkdir(parents=True);(output/"data").mkdir()
        try:
            client=ESClient(request.connection);self._update(task_id,status="running",message="连接 Elasticsearch")
            counts={item["index"]:int(item.get("docs.count") or 0) for item in client.indices()};total=max(1,sum(counts.get(i,0) for i in request.indexes));done=0
            for index in request.indexes:
                control.checkpoint();encoded=quote(index,safe="");safe="".join(c if c.isalnum() or c in "-_." else "_" for c in index)
                mapping=client.request("GET",f"/{encoded}/_mapping")
                (output/"mapping"/f"{safe}_mapping.json").write_text(json.dumps(mapping.get(index,mapping),ensure_ascii=False),"utf-8")
                page=client.request("POST",f"/{encoded}/_search?scroll=2m",json={"size":1000,"query":{"match_all":{}},"sort":["_doc"]})
                with (output/"data"/f"{safe}.json").open("w",encoding="utf-8") as file:
                    while True:
                        control.checkpoint();hits=page.get("hits",{}).get("hits",[])
                        if not hits:break
                        for hit in hits:file.write(json.dumps({"_id":hit.get("_id"),"_source":hit.get("_source",{})},ensure_ascii=False)+"\n")
                        done+=len(hits);self._update(task_id,progress=min(95,round(done/total*95,1)),message=f"正在导出 {index}：{done}/{total}")
                        scroll_id=page.get("_scroll_id")
                        if not scroll_id:break
                        page=client.request("POST","/_search/scroll",json={"scroll":"2m","scroll_id":scroll_id})
            archive=shutil.make_archive(str(task_dir),"zip",output)
            self._update(task_id,status="completed",progress=100,message="导出完成",result={"archive":Path(archive).name,"documents":done,"indexes":request.indexes})
        except TaskCancelled:self._update(task_id,status="cancelled",message="任务已取消")
        except Exception as exc:self._update(task_id,status="failed",message="导出失败",error=str(exc))
        finally:
            if control.cancelled:
                shutil.rmtree(task_dir,ignore_errors=True);(self.data_dir/f"{task_id}.zip").unlink(missing_ok=True)
            with self.lock:
                if self.controls.get(task_id) is control:self.controls.pop(task_id,None)

    def create_import(self,connection,upload_dir,overwrite):
        task_id,record=self._create_record("import",connection,"ES导入_"+upload_dir.name,{"overwrite":overwrite})
        task_dir=self.data_dir/task_id;task_dir.mkdir(parents=True);shutil.move(str(upload_dir),task_dir/"input")
        self.pool.submit(self._run_import,task_id,connection);return record

    def _run_import(self,task_id,connection):
        control=self.controls[task_id];task_dir=self.data_dir/task_id;root=task_dir/"input"
        try:
            archives=list(root.rglob("*.zip"))
            for archive in archives:
                target=root/(archive.stem+"_unzipped");target.mkdir()
                with zipfile.ZipFile(archive) as package:
                    for member in package.infolist():
                        destination=(target/member.filename).resolve()
                        if target.resolve() not in destination.parents and destination!=target.resolve():
                            raise ValueError("ZIP 包含不安全的文件路径")
                    package.extractall(target)
            data_files=[p for p in root.rglob("*.json") if p.parent.name=="data"]
            total=max(1,sum(sum(1 for _ in p.open(encoding="utf-8")) for p in data_files));done=0;client=ESClient(connection)
            self._update(task_id,status="running",message="连接 Elasticsearch")
            for data_file in data_files:
                control.checkpoint();index=data_file.stem;encoded=quote(index,safe="")
                mapping_files=list(root.rglob(f"{index}_mapping.json"))
                head=client.session.head(client.base+f"/{encoded}",timeout=30,verify=client.verify)
                if head.status_code not in (200,404):raise RuntimeError(f"ES {head.status_code}: {head.text[:500]}")
                exists=head.status_code==200
                if self.tasks[task_id]["request"]["overwrite"] and exists:client.request("DELETE",f"/{encoded}");exists=False
                if not exists:
                    body={}
                    if mapping_files:
                        raw=json.loads(mapping_files[0].read_text("utf-8"));body={"mappings":raw.get("mappings",raw.get(index,raw).get("mappings",{}))}
                    client.request("PUT",f"/{encoded}",json=body)
                batch=[]
                with data_file.open(encoding="utf-8") as file:
                    for line in file:
                        control.checkpoint();item=json.loads(line);source=item.get("_source",item);action={"index":{"_index":index}}
                        if item.get("_id") is not None:action["index"]["_id"]=item["_id"]
                        batch.extend([json.dumps(action,ensure_ascii=False),json.dumps(source,ensure_ascii=False)])
                        if len(batch)//2>=1000:
                            self._bulk(client,batch);done+=len(batch)//2;batch=[];self._update(task_id,progress=round(done/total*100,1),message=f"正在导入 {index}：{done}/{total}")
                    if batch:self._bulk(client,batch);done+=len(batch)//2
            self._update(task_id,status="completed",progress=100,message="导入完成",result={"documents":done,"indexes":[p.stem for p in data_files]})
        except TaskCancelled:self._update(task_id,status="cancelled",message="任务已取消")
        except Exception as exc:self._update(task_id,status="failed",message="导入失败",error=str(exc))
        finally:
            if control.cancelled:
                shutil.rmtree(task_dir,ignore_errors=True);(self.data_dir/f"{task_id}.zip").unlink(missing_ok=True)
            with self.lock:
                if self.controls.get(task_id) is control:self.controls.pop(task_id,None)

    @staticmethod
    def _bulk(client,batch):
        response=client.session.post(client.base+"/_bulk",data="\n".join(batch)+"\n",headers={"Content-Type":"application/x-ndjson"},timeout=120,verify=client.verify)
        if not response.ok:raise RuntimeError(f"Bulk {response.status_code}: {response.text[:500]}")
        result=response.json()
        if result.get("errors"):
            errors=[item for item in result.get("items",[]) if item.get("index",{}).get("error")][:3]
            raise RuntimeError("Bulk 写入失败: "+json.dumps(errors,ensure_ascii=False)[:1000])

    def control(self,task_id,action):
        with self.lock:
            task,control=self.tasks.get(task_id),self.controls.get(task_id)
            if not task:return None
            if action=="pause" and task["status"] in ("queued","running"):control.pause();task.update(status="paused",message="任务已暂停")
            elif action=="resume" and task["status"]=="paused":control.resume();task.update(status="running",message="任务继续执行")
            else:raise ValueError("当前状态不支持此操作")
            self._save();return task
    def delete(self,task_id):
        with self.lock:
            task=self.tasks.pop(task_id,None);control=self.controls.get(task_id)
            if not task:return False
            if control:control.cancel()
            self._save()
        shutil.rmtree(self.data_dir/task_id,ignore_errors=True);(self.data_dir/f"{task_id}.zip").unlink(missing_ok=True);return True
