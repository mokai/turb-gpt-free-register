#!/usr/bin/env python3
"""把另一个 turb.sqlite3 里的数据合并进当前项目的库。

设计要点
- 表结构为「索引列 + payload JSON」；payload 是权威数据，逐行原样搬运。
- 只追加、不覆盖：目标库已存在同 email 的行会被跳过（accounts / email_pool），
  registration_jobs 以 (email, created_at, job_uuid) 去重。
- id 重新分配为目标库当前最大值之后，避免主键冲突，也不改动已有行的 id。
- 默认先备份目标库（用 sqlite3 backup API，WAL 下也安全）。

用法
    python tools/migrate_db_from.py --src /path/to/old/turb.sqlite3            # 干跑，只看计划
    python tools/migrate_db_from.py --src /path/to/old/turb.sqlite3 --apply    # 实际写入（自动备份）
    python tools/migrate_db_from.py --src ... --apply --tables accounts        # 只迁指定表
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DST = Path(__file__).resolve().parent.parent / "turb.sqlite3"
DEFAULT_TABLES = ("accounts", "email_pool", "registration_jobs")

# 每张表的插入列（email_pool 多一个 source 列）
INSERT_SQL = {
    "accounts": "INSERT INTO accounts(id,email,status,archived,created_at,updated_at,payload) "
                "VALUES(?,?,?,?,?,?,?)",
    "registration_jobs": "INSERT INTO registration_jobs(id,email,status,archived,created_at,updated_at,payload) "
                         "VALUES(?,?,?,?,?,?,?)",
    "email_pool": "INSERT INTO email_pool(id,email,source,status,archived,created_at,updated_at,payload) "
                  "VALUES(?,?,?,?,?,?,?,?)",
}


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    """读取 payload（权威数据）。email_pool 的 source 是独立列、不在 payload 内，单独带出。"""
    if table == "email_pool":
        return [
            {**json.loads(r["payload"]), "_source": r["source"]}
            for r in conn.execute("SELECT payload, source FROM email_pool ORDER BY id")
        ]
    return [json.loads(r["payload"]) for r in conn.execute(f"SELECT payload FROM {table} ORDER BY id")]


def _meta(conn: sqlite3.Connection, table: str) -> tuple[int, set[str]]:
    """返回 (当前最大 id, 已存在行的去重键集合)。"""
    max_id = conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
    if table == "accounts":
        keys = {str(r["email"]).strip().lower() for r in conn.execute("SELECT email FROM accounts")}
    elif table == "email_pool":
        keys = {str(r["email"]).strip().lower() for r in conn.execute("SELECT email FROM email_pool")}
    else:  # registration_jobs：无唯一约束，用 payload 内三元组近似去重
        keys = {
            f"{j.get('email','')}|{j.get('created_at','')}|{j.get('job_uuid','')}"
            for j in _rows(conn, "registration_jobs")
        }
    return max_id, keys


def _key(table: str, row: dict) -> str:
    if table == "email_pool":
        return str(row.get("email") or "").strip().lower()
    if table == "registration_jobs":
        return f"{row.get('email','')}|{row.get('created_at','')}|{row.get('job_uuid','')}"
    return str(row.get("email") or "").strip().lower()


def _insert(conn: sqlite3.Connection, table: str, row: dict, source: str | None = None) -> None:
    row = {k: v for k, v in row.items() if k != "_source"}
    sql = INSERT_SQL[table]
    params: tuple = (
        row["id"], str(row.get("email") or ""), str(row.get("status") or ""),
        int(bool(row.get("archived"))),
        str(row.get("created_at") or row.get("imported_at") or ""),
        str(row.get("updated_at") or ""),
        json.dumps(row, ensure_ascii=False),
    )
    if table == "email_pool":
        # 列顺序：id,email,source,status,archived,created_at,updated_at,payload
        params = (params[0], params[1], str(source or row.get("source") or "unknown"),
                  params[2], params[3], params[4], params[5], params[6])
    conn.execute(sql, params)


def _backup(dst: Path) -> Path:
    target = dst.with_name(f"{dst.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    with _connect(dst) as src_conn:
        src_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with sqlite3.connect(str(target)) as out:
            src_conn.backup(out)
    # WAL / SHM 不需要带走：backup API 已产出自洽的单文件
    return target


def remap_job_logs(src: Path, src_log_dir: Path, dst: Path, dst_log_dir: Path, apply: bool) -> int:
    """把任务引用的日志文件复制到本项目，并把 payload.log_file 改写为新路径。

    registration_jobs 的日志是按 payload 里的绝对路径读取的，不改写会出现
    「任务在、日志打不开」。计划以源库为准（这样干跑也能预览），写入时按
    job_uuid 匹配目标库的行。
    """
    if not src_log_dir.is_dir():
        print(f"[提示] 源日志目录不存在，跳过日志迁移：{src_log_dir}")
        return 0

    src_conn = _connect(src)
    plan: dict[str, tuple[Path, Path]] = {}
    for payload in _rows(src_conn, "registration_jobs"):
        old = str(payload.get("log_file") or "")
        if old.startswith(str(src_log_dir)):
            plan[str(payload.get("job_uuid") or "")] = (Path(old), dst_log_dir / Path(old).name)
    src_conn.close()
    if not plan:
        print("\n[registration_jobs.log_file] 源库没有指向旧日志目录的记录，跳过")
        return 0

    print(f"\n[registration_jobs.log_file] 待迁移日志 {len(plan)} 个")
    missing = 0
    for old, new in list(plan.values())[:8]:
        if not old.exists():
            missing += 1
        print(f"    {old.name} → {new.name}（文件{'存在' if old.exists() else '缺失'}）")
    if len(plan) > 8:
        print(f"    … 其余 {len(plan) - 8} 个")
    if missing:
        print(f"    [注意] {missing} 个源日志文件缺失，只改写路径不复制文件")

    if apply:
        dst_log_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        for old, new in plan.values():
            if old.exists() and not new.exists():
                shutil.copy2(old, new)
                copied += 1
        with _connect(dst) as conn:
            updated = 0
            for row in conn.execute("SELECT id, payload FROM registration_jobs"):
                payload = json.loads(row["payload"])
                item = plan.get(str(payload.get("job_uuid") or ""))
                if item and str(payload.get("log_file") or "").startswith(str(src_log_dir)):
                    payload["log_file"] = str(item[1])
                    conn.execute("UPDATE registration_jobs SET payload=? WHERE id=?",
                                 (json.dumps(payload, ensure_ascii=False), row["id"]))
                    updated += 1
            conn.commit()
        print(f"    已复制 {copied} 个日志文件，改写 {updated} 条任务记录")
    return len(plan)


def migrate(src: Path, dst: Path, tables: list[str], apply: bool,
            src_log_dir: Path | None = None, dst_log_dir: Path | None = None) -> int:
    if not src.exists():
        print(f"[错误] 源库不存在：{src}")
        return 2
    if not dst.exists():
        print(f"[错误] 目标库不存在：{dst}")
        return 2

    backup_path = None
    if apply:
        backup_path = _backup(dst)
        print(f"[备份] 已备份目标库 → {backup_path}")

    source_conn, dest_conn = _connect(src), _connect(dst)
    total = 0
    try:
        for table in tables:
            src_rows = _rows(source_conn, table)
            base_id, existing = _meta(dest_conn, table)
            next_id = base_id + 1
            planned, skipped = [], 0
            for row in src_rows:
                if _key(table, row) in existing:
                    skipped += 1
                    continue
                row = dict(row)
                row["id"] = next_id
                next_id += 1
                planned.append(row)
            print(f"\n[{table}] 源 {len(src_rows)} 行 → 待插入 {len(planned)}，跳过重复 {skipped}"
                  f"（id 从 {base_id + 1} 起）")
            for row in planned[:10]:
                print(f"    + id={row['id']:<4} {str(row.get('email') or '(无邮箱)')}")
            if len(planned) > 10:
                print(f"    … 其余 {len(planned) - 10} 行")
            if apply and planned:
                with dest_conn:
                    for row in planned:
                        _insert(dest_conn, table, row, source=row.get("_source"))
                print(f"    已提交 {len(planned)} 行")
            total += len(planned)
    finally:
        source_conn.close()
        dest_conn.close()

    if src_log_dir is not None and dst_log_dir is not None:
        remap_job_logs(src, src_log_dir, dst, dst_log_dir, apply)

    mode = "已写入" if apply else "干跑（未写入）"
    print(f"\n[{mode}] 合计 {total} 行" + (f"，备份：{backup_path}" if backup_path else ""))
    if not apply:
        print("确认无误后加 --apply 实际写入。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="把旧 turb.sqlite3 的数据合并进当前库（只追加，不覆盖）")
    ap.add_argument("--src", required=True, help="源 turb.sqlite3 路径")
    ap.add_argument("--dst", default=str(DEFAULT_DST), help="目标 turb.sqlite3（默认当前项目）")
    ap.add_argument("--tables", default=",".join(DEFAULT_TABLES),
                    help=f"要迁移的表，逗号分隔（默认 {','.join(DEFAULT_TABLES)}）")
    ap.add_argument("--src-log-dir", default="",
                    help="源项目的「注册日志」目录；给了就把任务日志复制过来并改写路径")
    ap.add_argument("--apply", action="store_true", help="实际写入；不加则只做干跑")
    args = ap.parse_args()
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    unknown = [t for t in tables if t not in INSERT_SQL]
    if unknown:
        print(f"[错误] 不支持的表：{unknown}（可选：{list(INSERT_SQL)}）")
        return 2
    dst = Path(args.dst).expanduser()
    src_log_dir = Path(args.src_log_dir).expanduser() if args.src_log_dir else None
    return migrate(Path(args.src).expanduser(), dst, tables, args.apply,
                   src_log_dir=src_log_dir, dst_log_dir=dst.parent / "注册日志")


if __name__ == "__main__":
    sys.exit(main())
