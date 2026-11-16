"""SQLite 持久化：方案（含版本号）、执行记录。

并发模型：乐观锁。每个方案带 version，保存/确认时必须带上自己看到的版本号，
不一致则拒绝（409），后提交的计划员必须刷新后重来 —— 防止多人同时改方案时
互相悄悄覆盖。
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import time

import scheduler
import seed

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS scenarios(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',      -- draft | confirmed
  version INTEGER NOT NULL DEFAULT 1,
  assumptions TEXT NOT NULL,
  schedule TEXT,                              -- 最近一次计算结果（确认时快照）
  created_by TEXT, updated_by TEXT,
  parent_id INTEGER,
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS execution(
  phase_key TEXT PRIMARY KEY,
  project_id TEXT, phase_id TEXT, person_id TEXT,
  start_week REAL, end_week REAL,
  status TEXT,                                -- started | done
  note TEXT
);
"""


class VersionConflict(Exception):
    def __init__(self, current_version):
        super().__init__(f"版本冲突：当前版本为 v{current_version}")
        self.current_version = current_version


class NotFound(Exception):
    pass


class StateError(Exception):
    pass


def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def get_now(db):
    row = db.execute("SELECT value FROM meta WHERE key='now'").fetchone()
    return float(row["value"]) if row else seed.NOW


def init_db():
    """建库并写入种子数据（幂等）。"""
    db = get_db()
    if db.execute("SELECT value FROM meta WHERE key='seeded'").fetchone():
        return db
    now = seed.NOW
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('now', ?)", (str(now),))
    for ex in seed.seed_execution():
        db.execute(
            "INSERT INTO execution(phase_key, project_id, phase_id, person_id, start_week, end_week, status, note)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (ex["phase_key"], ex["project_id"], ex["phase_id"], ex["person_id"],
             ex["start_week"], ex["end_week"], ex["status"], ex["note"]),
        )
    execution = list_execution(db)
    t = time.time()
    seeds = [
        ("关键路径优先（基线）", "critical_path", "confirmed", "系统"),
        ("预期收益优先（基线）", "expected_benefit", "confirmed", "系统"),
        ("草案：收益优先+可调假设", "expected_benefit", "draft", "系统"),
    ]
    for name, strategy, status, by in seeds:
        assumptions = seed.base_assumptions(strategy)
        result = scheduler.schedule(assumptions, execution, now)
        db.execute(
            "INSERT INTO scenarios(name, status, version, assumptions, schedule, created_by, updated_by,"
            " parent_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (name, status, 1, json.dumps(assumptions, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), by, by, None, t, t),
        )
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('seeded', '1')")
    db.commit()
    return db


# ---------- 查询 ----------

def list_scenarios(db):
    rows = db.execute(
        "SELECT id, name, status, version, updated_by, updated_at, parent_id FROM scenarios ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_scenario(db, sid):
    row = db.execute("SELECT * FROM scenarios WHERE id=?", (sid,)).fetchone()
    if not row:
        raise NotFound(f"方案 {sid} 不存在")
    d = dict(row)
    d["assumptions"] = json.loads(d["assumptions"])
    d["schedule"] = json.loads(d["schedule"]) if d["schedule"] else None
    return d


def list_execution(db):
    rows = db.execute("SELECT * FROM execution ORDER BY start_week").fetchall()
    return [dict(r) for r in rows]


# ---------- 变更 ----------

def create_scenario(db, name, assumptions, by, parent_id=None, compute=True):
    t = time.time()
    schedule_json = None
    if compute:
        result = scheduler.schedule(assumptions, list_execution(db), get_now(db))
        schedule_json = json.dumps(result, ensure_ascii=False)
    cur = db.execute(
        "INSERT INTO scenarios(name, status, version, assumptions, schedule, created_by, updated_by,"
        " parent_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (name, "draft", 1, json.dumps(assumptions, ensure_ascii=False), schedule_json, by, by, parent_id, t, t),
    )
    db.commit()
    return cur.lastrowid


def update_assumptions(db, sid, version, assumptions, by):
    """保存草案假设（乐观锁），并立即重算。返回最新行。"""
    row = get_scenario(db, sid)
    if row["status"] != "draft":
        raise StateError("只有草案可以编辑；已确认方案请使用「恢复为草案」")
    if int(version) != row["version"]:
        raise VersionConflict(row["version"])
    result = scheduler.schedule(assumptions, list_execution(db), get_now(db))
    db.execute(
        "UPDATE scenarios SET assumptions=?, schedule=?, version=version+1, updated_by=?, updated_at=? WHERE id=?",
        (json.dumps(assumptions, ensure_ascii=False), json.dumps(result, ensure_ascii=False), by, time.time(), sid),
    )
    db.commit()
    return get_scenario(db, sid)


def recompute(db, sid):
    row = get_scenario(db, sid)
    if row["status"] != "draft":
        raise StateError("已确认方案保留确认时的快照，不能重算；如需调整请「恢复为草案」")
    result = scheduler.schedule(row["assumptions"], list_execution(db), get_now(db))
    db.execute("UPDATE scenarios SET schedule=?, updated_at=? WHERE id=?",
               (json.dumps(result, ensure_ascii=False), time.time(), sid))
    db.commit()
    return get_scenario(db, sid)


def confirm(db, sid, version, by):
    """确认方案：版本校验 + 冻结阶段校验，通过后把当前计算结果固化为快照。"""
    row = get_scenario(db, sid)
    if row["status"] != "draft":
        raise StateError("该方案已是确认状态")
    if int(version) != row["version"]:
        raise VersionConflict(row["version"])
    result = scheduler.schedule(row["assumptions"], list_execution(db), get_now(db))
    if result["frozen_violations"]:
        raise StateError("已开始/已完成的阶段被方案修改，不能确认：" + "；".join(result["frozen_violations"]))
    db.execute(
        "UPDATE scenarios SET status='confirmed', schedule=?, version=version+1, updated_by=?, updated_at=? WHERE id=?",
        (json.dumps(result, ensure_ascii=False), by, time.time(), sid),
    )
    db.commit()
    return get_scenario(db, sid)


def restore_as_draft(db, sid, by):
    """把任意方案（含已确认）的假设恢复为一个新草案并重算。"""
    row = get_scenario(db, sid)
    assumptions = copy.deepcopy(row["assumptions"])
    return create_scenario(db, row["name"] + "（恢复）", assumptions, by, parent_id=sid)


def add_execution(db, record):
    db.execute(
        "INSERT INTO execution(phase_key, project_id, phase_id, person_id, start_week, end_week, status, note)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (record["phase_key"], record["project_id"], record["phase_id"], record["person_id"],
         record["start_week"], record["end_week"], record["status"], record.get("note", "")),
    )
    db.commit()
