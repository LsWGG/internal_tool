import importlib.util
import json
import shutil
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .models import NebulaConnection, NebulaExportTask


def load_utility(path: Path):
    spec = importlib.util.spec_from_file_location("nebula_data_util_web", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module.NebulaDataUtil


class NebulaTaskManager:
    def __init__(self, data_dir: Path, utility_path: Path):
        self.data_dir=data_dir;data_dir.mkdir(parents=True,exist_ok=True);self.utility=load_utility(utility_path)
        self.state_file=data_dir/"tasks.json";self.lock=threading.RLock();self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix="nebula-task");self.cancelled=set()
        try:self.tasks=json.loads(self.state_file.read_text("utf-8"))
        except (FileNotFoundError,json.JSONDecodeError):self.tasks={}
        for task in self.tasks.values():
            if task["status"] in ("queued","running"):task.update(status="failed",message="服务重启，任务已中断")
        self._save()

    def _save(self):
        self.data_dir.mkdir(parents=True,exist_ok=True)
        tmp=self.state_file.with_suffix(".tmp");tmp.write_text(json.dumps(self.tasks,ensure_ascii=False,indent=2),"utf-8");tmp.replace(self.state_file)
    def _update(self,task_id,**values):
        with self.lock:
            if task_id not in self.tasks:return
            values["updated_at"]=datetime.now(timezone.utc).isoformat();self.tasks[task_id].update(values);self._save()
    def list(self):
        with self.lock:return sorted(self.tasks.values(),key=lambda x:x["created_at"],reverse=True)
    def get(self,task_id):
        with self.lock:return self.tasks.get(task_id)
    def _record(self,mode,connection,name,details):
        task_id,now=uuid.uuid4().hex,datetime.now(timezone.utc).isoformat()
        task={"id":task_id,"mode":mode,"name":name,"status":"queued","progress":0,"message":"等待执行","created_at":now,"updated_at":now,
              "request":{"host":connection.host,"port":connection.port,"username":connection.username,**details},"result":None,"error":None}
        with self.lock:self.cancelled.discard(task_id);self.tasks[task_id]=task;self._save()
        return task_id,task
    def _cancelled(self,task_id):
        with self.lock:return task_id in self.cancelled or task_id not in self.tasks
    def connect(self,connection):
        util=self.utility(connection.host,connection.port,connection.username,connection.password);session=util.session_pool.get_session()
        try:
            response=session.do_execute("SHOW SPACES;")
            return [value.as_string() for value in response.column_values("Name")]
        finally:session.release();util.session_pool.close()
    def create_export(self,request:NebulaExportTask):
        task_id,task=self._record("export",request.connection,"Nebula导出_"+"_".join(request.spaces[:3]),{"spaces":request.spaces})
        self.pool.submit(self._run_export,task_id,request);return task
    def _run_export(self,task_id,request):
        task_dir=self.data_dir/task_id;output=task_dir/"output";output.mkdir(parents=True)
        util=None
        try:
            if self._cancelled(task_id):return
            util=self.utility(request.connection.host,request.connection.port,request.connection.username,request.connection.password)
            total=len(request.spaces)
            for pos,space in enumerate(request.spaces,1):
                if self._cancelled(task_id):return
                self._update(task_id,status="running",progress=round((pos-1)/total*90,1),message=f"正在导出 Space：{space}")
                util.export_nebula([space],str(output),max_size=2_147_483_647)
            util.session_pool.close()
            if self._cancelled(task_id):return
            archive=shutil.make_archive(str(task_dir),"zip",output)
            if self._cancelled(task_id):return
            self._update(task_id,status="completed",progress=100,message="导出完成",result={"spaces":request.spaces,"archive":Path(archive).name})
        except Exception as exc:self._update(task_id,status="failed",message="导出失败",error=str(exc))
        finally:
            if util:
                try:util.session_pool.close()
                except Exception:pass
            if self._cancelled(task_id):shutil.rmtree(task_dir,ignore_errors=True);(self.data_dir/f"{task_id}.zip").unlink(missing_ok=True)
            with self.lock:self.cancelled.discard(task_id)
    def create_import(self,connection,upload_dir):
        task_id,task=self._record("import",connection,"Nebula导入_"+upload_dir.name,{})
        task_dir=self.data_dir/task_id;task_dir.mkdir(parents=True);shutil.move(str(upload_dir),task_dir/"input");self.pool.submit(self._run_import,task_id,connection);return task
    def _run_import(self,task_id,connection):
        root=self.data_dir/task_id/"input"
        util=None
        try:
            if self._cancelled(task_id):return
            for archive in root.rglob("*.zip"):
                target=root/(archive.stem+"_unzipped");target.mkdir()
                with zipfile.ZipFile(archive) as package:
                    for member in package.infolist():
                        destination=(target/member.filename).resolve()
                        if target.resolve() not in destination.parents and destination!=target.resolve():raise ValueError("ZIP 包含不安全的文件路径")
                    package.extractall(target)
            bases=list(root.rglob("*_base.txt"))
            if not bases:raise ValueError("未找到 Nebula 备份文件 *_base.txt")
            backup_root=bases[0].parent.parent
            self._update(task_id,status="running",progress=10,message="正在创建 Space、Tag 和 Edge Schema")
            util=self.utility(connection.host,connection.port,connection.username,connection.password);util.import_nebula(str(backup_root),chunk_size=100);util.session_pool.close()
            if self._cancelled(task_id):return
            self._update(task_id,status="completed",progress=100,message="导入完成",result={"spaces":[p.parent.name for p in bases]})
        except Exception as exc:self._update(task_id,status="failed",message="导入失败",error=str(exc))
        finally:
            if util:
                try:util.session_pool.close()
                except Exception:pass
            if self._cancelled(task_id):shutil.rmtree(self.data_dir/task_id,ignore_errors=True);(self.data_dir/f"{task_id}.zip").unlink(missing_ok=True)
            with self.lock:self.cancelled.discard(task_id)
    def delete(self,task_id):
        with self.lock:
            if not self.tasks.pop(task_id,None):return False
            self.cancelled.add(task_id)
            self._save()
        shutil.rmtree(self.data_dir/task_id,ignore_errors=True);(self.data_dir/f"{task_id}.zip").unlink(missing_ok=True);return True
