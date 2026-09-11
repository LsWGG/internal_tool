"""PostgreSQL sink for long-running crawler tasks."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime


def validate_config(value):
    config = dict(value or {})
    if not config.get("enabled"):
        return {"enabled": False}
    config["type"] = "postgresql"
    config["host"] = str(config.get("host") or "").strip()
    config["database"] = str(config.get("database") or "").strip()
    config["username"] = str(config.get("username") or "").strip()
    config["password"] = str(config.get("password") or "")
    config["port"] = int(config.get("port") or 5432)
    config["sslmode"] = str(config.get("sslmode") or "prefer")
    config["schema"] = str(config.get("schema") or "public").strip()
    config["table"] = str(config.get("table") or "crawler_data").strip()
    if not config["host"] or not config["database"]:
        raise ValueError("请填写 PostgreSQL 主机和数据库名")
    if not 1 <= config["port"] <= 65535:
        raise ValueError("PostgreSQL 端口不正确")
    for name in ("schema", "table"):
        if not config[name] or len(config[name]) > 63 or "\x00" in config[name]:
            raise ValueError(f"PostgreSQL {name} 名称不正确")
    return config


def _text(value):
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _key(row):
    url = str(row.get("url") or "").strip()
    if url:
        return "url:" + hashlib.sha256(url.encode("utf-8")).hexdigest()
    stable = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return "content:" + hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _column_name(name, occupied):
    raw = str(name or "field").strip()
    base = re.sub(r"[^a-zA-Z0-9_]+", "_", raw).strip("_").lower()
    if not base or base[0].isdigit():
        base = "field_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    base = base[:55]
    candidate = base
    suffix = 1
    while candidate in occupied or candidate in {"id", "crawler_key", "crawled_at"}:
        suffix += 1
        candidate = f"{base[:51]}_{suffix}"
    occupied.add(candidate)
    return candidate


def test_connection(value):
    from .database_manager import DatabaseClient
    config = validate_config({**dict(value or {}), "enabled": True})
    client = DatabaseClient(config)
    try:
        info = client.ping()
        return {"ok": True, "message": "PostgreSQL 连接成功", **info}
    finally:
        client.close()


def write_rows(value, rows):
    import psycopg
    from psycopg import sql

    config = validate_config(value)
    if not config.get("enabled") or not rows:
        return {"inserted": 0, "duplicates": 0, "table": ""}
    connection = psycopg.connect(
        host=config["host"], port=config["port"], dbname=config["database"],
        user=config.get("username") or None, password=config.get("password") or None,
        sslmode=config["sslmode"], connect_timeout=15,
    )
    try:
        schema, table = config["schema"], config["table"]
        fields = list(dict.fromkeys(str(key) for row in rows for key in row.keys() if key != "video_file"))
        occupied = set()
        columns = {field: _column_name(field, occupied) for field in fields}
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            cursor.execute(sql.SQL("""CREATE TABLE IF NOT EXISTS {}.{} (
                id BIGSERIAL PRIMARY KEY,
                crawler_key TEXT NOT NULL UNIQUE,
                crawled_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""").format(sql.Identifier(schema), sql.Identifier(table)))
            for column in columns.values():
                cursor.execute(sql.SQL("ALTER TABLE {}.{} ADD COLUMN IF NOT EXISTS {} TEXT").format(
                    sql.Identifier(schema), sql.Identifier(table), sql.Identifier(column)))
            inserted = 0
            names = ["crawler_key", *columns.values()]
            statement = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({}) ON CONFLICT (crawler_key) DO NOTHING").format(
                sql.Identifier(schema), sql.Identifier(table),
                sql.SQL(", ").join(map(sql.Identifier, names)),
                sql.SQL(", ").join(sql.Placeholder() for _ in names),
            )
            for row in rows:
                cursor.execute(statement, [_key(row), *(_text(row.get(field)) for field in fields)])
                inserted += max(cursor.rowcount, 0)
        connection.commit()
        return {"inserted": inserted, "duplicates": len(rows) - inserted,
                "table": f"{schema}.{table}", "columns": columns}
    finally:
        connection.close()
