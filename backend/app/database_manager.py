import json
import shutil
import sqlite3
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path


def _identifier(value: str):
    value = str(value or "").strip()
    if not value or "\x00" in value or len(value) > 255:
        raise ValueError(f"非法标识符：{value}")
    return '"' + value.replace('"', '""') + '"'


def _json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return value


class DatabaseClient:
    def __init__(self, config: dict):
        self.config = self._validate(config)
        self.kind = self.config["type"]
        self.connection = None

    @staticmethod
    def _validate(config):
        if not isinstance(config, dict):
            raise ValueError("数据库配置格式错误")
        kind = str(config.get("type", "sqlite")).lower()
        if kind not in ("sqlite", "postgresql", "redis"):
            raise ValueError("暂不支持该数据库类型")
        result = dict(config)
        result["type"] = kind
        if kind == "sqlite":
            path = str(result.get("database") or result.get("path") or "").strip()
            if not path:
                raise ValueError("SQLite 需要填写数据库文件路径")
            if path != ":memory:":
                result["database"] = str(Path(path).expanduser().resolve())
        elif kind == "postgresql":
            if not str(result.get("host") or "").strip():
                raise ValueError("PostgreSQL 需要填写主机地址")
            result["port"] = int(result.get("port") or 5432)
            if not str(result.get("database") or "").strip():
                raise ValueError("PostgreSQL 需要填写数据库名")
        else:
            result["port"] = int(result.get("port") or 6379)
        return result

    def open(self):
        if self.connection is not None:
            return self.connection
        if self.kind == "sqlite":
            self.connection = sqlite3.connect(self.config["database"], timeout=30)
            self.connection.row_factory = sqlite3.Row
        elif self.kind == "postgresql":
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:
                raise RuntimeError("未安装 PostgreSQL 驱动 psycopg，请安装项目依赖后重启服务") from exc
            self.connection = psycopg.connect(
                host=self.config["host"], port=self.config["port"], dbname=self.config["database"],
                user=self.config.get("username") or None, password=self.config.get("password") or None,
                sslmode=self.config.get("sslmode") or "prefer", row_factory=dict_row,
            )
        else:
            try:
                import redis
            except ImportError as exc:
                raise RuntimeError("未安装 Redis 驱动 redis，请安装项目依赖后重启服务") from exc
            if self.config.get("url"):
                self.connection = redis.Redis.from_url(self.config["url"], decode_responses=False)
            else:
                self.connection = redis.Redis(
                    host=self.config.get("host") or "127.0.0.1", port=self.config["port"],
                    username=self.config.get("username") or None, password=self.config.get("password") or None,
                    db=int(self.config.get("database") or 0), decode_responses=False,
                )
        return self.connection

    def close(self):
        if self.connection is None:
            return
        try:
            self.connection.close()
        finally:
            self.connection = None

    def ping(self):
        connection = self.open()
        if self.kind == "sqlite":
            connection.execute("SELECT 1").fetchone()
            return {"type": "sqlite", "database": self.config["database"]}
        if self.kind == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT version()")
                version = cursor.fetchone()["version"]
            return {"type": "postgresql", "version": version}
        connection.ping()
        return {"type": "redis", "database": int(self.config.get("database") or 0)}

    def tables(self):
        connection = self.open()
        if self.kind == "redis":
            keys = [self._decode(key) for key in connection.scan_iter(match=self.config.get("key_pattern") or "*", count=500)]
            return [{"name": key, "kind": "key"} for key in keys[:5000]]
        if self.kind == "sqlite":
            rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            return [{"name": row[0], "kind": "table"} for row in rows]
        with connection.cursor() as cursor:
            cursor.execute("SELECT tablename AS name FROM pg_catalog.pg_tables WHERE schemaname='public' ORDER BY tablename")
            return [{"name": row["name"], "kind": "table"} for row in cursor.fetchall()]

    def schema(self, table: str):
        if self.kind == "redis":
            return {"name": table, "kind": "key", "columns": [{"name": "key"}, {"name": "value"}]}
        connection = self.open(); quoted = _identifier(table)
        if self.kind == "sqlite":
            rows = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
            if not rows:
                raise ValueError(f"表不存在：{table}")
            return {"name": table, "kind": "table", "columns": [{"name": row[1], "type": row[2], "primary_key": bool(row[5]), "nullable": not bool(row[3]) and not bool(row[5])} for row in rows]}
        with connection.cursor() as cursor:
            cursor.execute("SELECT column_name, data_type, is_nullable, (column_name IN (SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) WHERE i.indrelid=%s::regclass AND i.indisprimary)) AS primary_key FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position", (table, table))
            rows = cursor.fetchall()
        if not rows:
            raise ValueError(f"表不存在：{table}")
        return {"name": table, "kind": "table", "columns": [{"name": row["column_name"], "type": row["data_type"], "primary_key": bool(row["primary_key"]), "nullable": row["is_nullable"] == "YES"} for row in rows]}

    @staticmethod
    def _decode(value):
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    def rows(self, table: str, limit=50, offset=0):
        limit = max(1, min(int(limit or 50), 1_000_000)); offset = max(0, int(offset or 0))
        connection = self.open()
        if self.kind == "redis":
            value = connection.get(table)
            return [{"key": table, "value": self._decode(value)}] if value is not None else []
        query = f"SELECT * FROM {_identifier(table)} LIMIT ? OFFSET ?" if self.kind == "sqlite" else f"SELECT * FROM {_identifier(table)} LIMIT %s OFFSET %s"
        cursor = connection.execute(query, (limit, offset)) if self.kind == "sqlite" else connection.cursor()
        if self.kind == "postgresql":
            cursor.execute(query, (limit, offset)); result = cursor.fetchall(); cursor.close()
        else:
            result = cursor.fetchall()
        return [{key: _json_value(value) for key, value in dict(row).items()} for row in result]

    def count(self, table: str):
        connection = self.open()
        if self.kind == "redis":
            return int(connection.exists(table))
        query = f"SELECT COUNT(*) AS count FROM {_identifier(table)}"
        if self.kind == "sqlite":
            return int(connection.execute(query).fetchone()[0])
        with connection.cursor() as cursor:
            cursor.execute(query)
            return int(cursor.fetchone()["count"])

    def _commit(self):
        if self.kind != "redis":
            self.connection.commit()

    def crud(self, action: str, table: str, values=None, where=None):
        values, where = dict(values or {}), dict(where or {})
        values = {key: (bytes.fromhex(value["__bytes__"]) if isinstance(value, dict) and "__bytes__" in value else value) for key, value in values.items()}
        where = {key: (bytes.fromhex(value["__bytes__"]) if isinstance(value, dict) and "__bytes__" in value else value) for key, value in where.items()}
        connection = self.open()
        if self.kind == "redis":
            original_key = str(where.get("key") or table)
            key = str(values.get("key") or original_key)
            if action == "create" or action == "update":
                if "value" not in values: raise ValueError("Redis 操作需要 value")
                connection.set(key, json.dumps(values["value"], ensure_ascii=False) if isinstance(values["value"], (dict, list)) else str(values["value"]))
                if action == "update" and original_key != key:
                    connection.delete(original_key)
                return {"key": key, "value": values["value"]}
            if action == "delete":
                return {"deleted": int(connection.delete(key)), "key": key}
            if action == "read":
                return {"key": key, "value": self._decode(connection.get(key))}
            raise ValueError("不支持的 Redis 操作")
        info = self.schema(table); allowed = {item["name"] for item in info["columns"]}
        if action == "create":
            if not values: raise ValueError("新增记录不能为空")
            columns = [key for key in values if key in allowed]
            if len(columns) != len(values): raise ValueError("包含不存在的字段")
            mark = ",".join("?" for _ in columns) if self.kind == "sqlite" else ",".join("%s" for _ in columns)
            query = f"INSERT INTO {_identifier(table)} ({','.join(_identifier(c) for c in columns)}) VALUES ({mark})"
            self.connection.execute(query, tuple(values[c] for c in columns)); self._commit(); return {"inserted": 1}
        if any(key not in allowed for key in where): raise ValueError("筛选条件包含不存在的字段")
        marker = "?" if self.kind == "sqlite" else "%s"
        condition_parts, condition_values = [], []
        for key, value in where.items():
            if value is None:
                condition_parts.append(f"{_identifier(key)} IS NULL")
            else:
                condition_parts.append(f"{_identifier(key)}={marker}")
                condition_values.append(value)
        condition = " AND ".join(condition_parts)
        if action == "read":
            query = f"SELECT * FROM {_identifier(table)}"
            params = tuple(condition_values)
            if condition:
                query += f" WHERE {condition}"
            query += " LIMIT 100"
            cursor = self.connection.execute(query, params) if self.kind == "sqlite" else self.connection.cursor()
            if self.kind == "postgresql":
                cursor.execute(query, params); result = cursor.fetchall(); cursor.close()
            else:
                result = cursor.fetchall()
            return {"rows": [{key: _json_value(value) for key, value in dict(row).items()} for row in result]}
        if not where: raise ValueError("更新或删除必须提供筛选条件")
        if action == "update":
            if not values: raise ValueError("更新内容不能为空")
            if any(key not in allowed for key in values): raise ValueError("更新内容包含不存在的字段")
            assignments = ",".join(f"{_identifier(key)}={marker}" for key in values)
            query = f"UPDATE {_identifier(table)} SET {assignments} WHERE {condition}"
            cursor = self.connection.execute(query, tuple(values.values()) + tuple(condition_values)); self._commit(); return {"updated": cursor.rowcount}
        if action == "delete":
            cursor = self.connection.execute(f"DELETE FROM {_identifier(table)} WHERE {condition}", tuple(condition_values)); self._commit(); return {"deleted": cursor.rowcount}
        raise ValueError("不支持的 CRUD 操作")

    def export_payload(self, tables=None):
        self.ping()
        if self.kind == "redis":
            selected = tables or [item["name"] for item in self.tables()]
            return {"version": 1, "type": "redis", "keys": [self.crud("read", key) for key in selected]}
        selected = tables or [item["name"] for item in self.tables()]
        tables = []
        for name in selected:
            all_rows = []
            offset = 0
            while True:
                chunk = self.rows(name, 1000, offset)
                all_rows.extend(chunk)
                if len(chunk) < 1000:
                    break
                offset += len(chunk)
            tables.append({"name": name, "schema": self.schema(name), "rows": all_rows})
        return {"version": 1, "type": self.kind, "tables": tables}

    def import_payload(self, payload, overwrite=False):
        kind = payload.get("type")
        if self.kind == "redis":
            for item in payload.get("keys", []):
                if item.get("value") is not None: self.crud("update", item.get("key", ""), {"key": item.get("key"), "value": item.get("value")})
            return {"keys": len(payload.get("keys", []))}
        imported = 0; connection = self.open()
        for table in payload.get("tables", []):
            name = table.get("name"); columns = table.get("schema", {}).get("columns", []); quoted = _identifier(name)
            exists = any(item["name"] == name for item in self.tables())
            if exists and overwrite:
                connection.execute(f"DELETE FROM {quoted}")
            elif not exists:
                definitions = []
                for column in columns:
                    col_type = str(column.get("type") or "TEXT").upper()
                    if self.kind == "sqlite" and col_type not in ("INTEGER", "REAL", "TEXT", "BLOB", "NUMERIC"):
                        col_type = "TEXT"
                    definitions.append(f"{_identifier(column['name'])} {col_type}{' PRIMARY KEY' if column.get('primary_key') else ''}")
                if not definitions: continue
                connection.execute(f"CREATE TABLE {quoted} ({', '.join(definitions)})")
            for row in table.get("rows", []):
                values = {key: (bytes.fromhex(value["__bytes__"]) if isinstance(value, dict) and "__bytes__" in value else value) for key, value in row.items()}
                if values:
                    self.crud("create", name, values)
                    imported += 1
        self._commit(); return {"rows": imported, "tables": len(payload.get("tables", []))}


class DatabaseTaskManager:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve(); self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "tasks.json"; self.lock = threading.RLock(); self.runtime = {}; self.cancelled = set()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="database-task")
        try: self.tasks = json.loads(self.state_file.read_text("utf-8"))
        except Exception: self.tasks = {}
        for task in self.tasks.values():
            if task.get("status") in ("queued", "running"): task.update(status="failed", message="服务重启，任务已中断", error="请重新创建任务")
        self._save()

    def _save(self):
        temp = self.state_file.with_suffix(".tmp"); temp.write_text(json.dumps(self.tasks, ensure_ascii=False, indent=2), "utf-8"); temp.replace(self.state_file)
    @staticmethod
    def _safe_config(config): return {key: value for key, value in config.items() if key not in ("password",)}
    def _update(self, task_id, **values):
        with self.lock:
            if task_id in self.tasks and task_id not in self.cancelled:
                self.tasks[task_id].update(values, updated_at=datetime.now(timezone.utc).isoformat()); self._save()
    def list(self):
        with self.lock: return sorted(self.tasks.values(), key=lambda item: item["created_at"], reverse=True)
    def get(self, task_id):
        with self.lock: return self.tasks.get(task_id)
    def create(self, mode, config, tables, overwrite=False, upload_dir=None):
        task_id = uuid.uuid4().hex; now = datetime.now(timezone.utc).isoformat(); kind = config.get("type", "sqlite")
        name = f"{kind.upper()} {'导出' if mode == 'export' else '导入'}"
        record = {"id": task_id, "name": name, "mode": mode, "status": "queued", "progress": 0, "message": "等待执行", "created_at": now, "updated_at": now, "request": {"type": kind, "tables": tables, "overwrite": overwrite}, "result": None, "error": None}
        with self.lock: self.tasks[task_id] = record; self.runtime[task_id] = dict(config); self._save()
        if upload_dir:
            task_dir = self.data_dir / task_id; task_dir.mkdir(parents=True); shutil.move(str(upload_dir), task_dir / "input")
        self.pool.submit(self._run, task_id, mode, tables, overwrite); return record
    def _run(self, task_id, mode, tables, overwrite):
        config = self.runtime.get(task_id); task_dir = self.data_dir / task_id; archive = self.data_dir / f"{task_id}.zip"
        try:
            self._update(task_id, status="running", progress=5, message="正在连接数据库")
            client = DatabaseClient(config)
            if mode == "export":
                payload = client.export_payload(tables); self._update(task_id, progress=70, message="正在写入备份文件")
                task_dir.mkdir(parents=True, exist_ok=True); (task_dir / "database.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package: package.write(task_dir / "database.json", "database.json")
                result = {"archive": archive.name, "tables": len(payload.get("tables", payload.get("keys", []))), "rows": sum(len(item.get("rows", [])) for item in payload.get("tables", []))}
            else:
                package = next((item for item in task_dir.rglob("*.zip")), None)
                if package:
                    with zipfile.ZipFile(package) as source: payload = json.loads(source.read("database.json"))
                else: payload = json.loads(next(task_dir.rglob("database.json")).read_text("utf-8"))
                result = client.import_payload(payload, overwrite); self._update(task_id, progress=90, message="正在提交导入结果")
            client.close(); self._update(task_id, status="completed", progress=100, message="导出完成" if mode == "export" else "导入完成", result=result, error=None)
        except Exception as exc:
            self._update(task_id, status="failed", progress=0, message="导出失败" if mode == "export" else "导入失败", error=str(exc))
        finally:
            with self.lock:
                if task_id in self.cancelled: self.cancelled.discard(task_id); self.runtime.pop(task_id, None)
    def retry(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id); config = self.runtime.get(task_id)
            if not task: return None
            if task["status"] != "failed": raise ValueError("仅失败任务可以重试")
            if not config: raise ValueError("任务连接信息已过期，请重新创建任务")
            task.update(status="queued", progress=0, message="等待重试", error=None, result=None); self._save(); self.pool.submit(self._run, task_id, task["mode"], task["request"].get("tables", []), task["request"].get("overwrite", False)); return task
    def delete(self, task_id):
        with self.lock:
            if task_id not in self.tasks: return False
            self.cancelled.add(task_id); self.tasks.pop(task_id); self.runtime.pop(task_id, None); self._save()
        shutil.rmtree(self.data_dir / task_id, ignore_errors=True); (self.data_dir / f"{task_id}.zip").unlink(missing_ok=True); return True
