# -*- coding: utf-8 -*-
"""SQLite 连接、建表与初始化。"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from flask import current_app, g

from .seed_data import seed_if_empty

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    file        TEXT NOT NULL,
    w           INTEGER NOT NULL,
    h           INTEGER NOT NULL,
    is_base     INTEGER NOT NULL DEFAULT 0,
    tx          REAL NOT NULL,
    ty          REAL NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS alignments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT UNIQUE,
    image_a     TEXT NOT NULL REFERENCES images(id),
    image_b     TEXT NOT NULL REFERENCES images(id),
    ax          REAL NOT NULL,
    ay          REAL NOT NULL,
    bx          REAL NOT NULL,
    by          REAL NOT NULL,
    reviewed    INTEGER NOT NULL DEFAULT 0,   -- 1 已复核
    status      TEXT NOT NULL DEFAULT 'active', -- active / withdrawn
    origin      TEXT NOT NULL DEFAULT 'seed',   -- seed / submitted
    note        TEXT,
    run_id      INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS cracks (
    id          TEXT PRIMARY KEY,
    image_id    TEXT NOT NULL REFERENCES images(id),
    code        TEXT NOT NULL,
    x           REAL NOT NULL,
    y           REAL NOT NULL,
    tagged      INTEGER NOT NULL DEFAULT 0,     -- 1 已贴签
    note        TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    status       TEXT NOT NULL,                 -- applied / no_solution
    image_a      TEXT,
    image_b      TEXT,
    ax           REAL, ay REAL, bx REAL, by REAL,
    payload_json TEXT,
    result_json  TEXT,
    summary      TEXT
);

CREATE INDEX IF NOT EXISTS idx_align_status ON alignments(status);
CREATE INDEX IF NOT EXISTS idx_cracks_image ON cracks(image_id);

-- 全局元信息：对位版本号。每次“有解并应用”的对位更新 +1。
CREATE TABLE IF NOT EXISTS app_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

-- 现场复核批次：开始时记录对位版本。
CREATE TABLE IF NOT EXISTS review_batches (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no          TEXT UNIQUE,             -- 批次号 PC-0001 ...
    alignment_version INTEGER NOT NULL,        -- 开始时的对位版本
    note              TEXT,
    status            TEXT NOT NULL DEFAULT 'open', -- open（进行中；按条目派生进度）
    created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 批次内的每条裂缝：批次快照总图坐标 + 复核状态机。
-- 一个批次中同一条裂缝只有一行（重复提交只保留这一次确认）。
CREATE TABLE IF NOT EXISTS review_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER NOT NULL REFERENCES review_batches(id),
    crack_id        TEXT NOT NULL REFERENCES cracks(id),
    crack_code      TEXT NOT NULL,
    snap_x          REAL NOT NULL,             -- 开始/重开时的总图快照坐标
    snap_y          REAL NOT NULL,
    snap_version    INTEGER NOT NULL,          -- 快照对应的对位版本
    status          TEXT NOT NULL DEFAULT 'pending', -- pending 待复核 / confirmed 已确认 / expired 已过期
    field_x         REAL,                      -- 首次确认的现场坐标（历史保留，不被覆盖）
    field_y         REAL,
    deviation       REAL,                      -- 确认时与快照的像素偏差
    round_no        INTEGER NOT NULL DEFAULT 1,-- 重开一次 +1，用于区分新一轮的拒绝原因
    confirmed_at    TEXT,
    expired_at      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT,
    UNIQUE(batch_id, crack_id)
);

-- 提交/拒绝审计流水：无效提交只允许追加本表，不得改动 review_items 状态。
CREATE TABLE IF NOT EXISTS review_submissions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      INTEGER NOT NULL REFERENCES review_batches(id),
    item_id       INTEGER NOT NULL REFERENCES review_items(id),
    crack_code    TEXT NOT NULL,
    round_no      INTEGER NOT NULL DEFAULT 1,
    field_x       REAL,
    field_y       REAL,
    deviation     REAL,
    accepted      INTEGER NOT NULL DEFAULT 0,  -- 0 拒绝（本表现阶段只记拒绝）
    reason_code   TEXT,                        -- too_far / expired / ...
    reason        TEXT,                        -- 页面展示的中文拒绝原因
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_review_items_batch ON review_items(batch_id);
CREATE INDEX IF NOT EXISTS idx_review_items_status ON review_items(status);
CREATE INDEX IF NOT EXISTS idx_review_sub_item ON review_submissions(item_id);
"""


def default_db_path() -> Path:
    env = os.environ.get("MURAL_DB")
    if env:
        return Path(env)
    # 仓库根/data/mural.db（app/ 的上一级）
    return Path(__file__).resolve().parent.parent / "data" / "mural.db"


def connect(db_path: str | os.PathLike | None = None) -> sqlite3.Connection:
    path = Path(db_path or current_app.config["DATABASE"])
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 5000")
    return db


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(db_path: str | os.PathLike | None = None) -> Path:
    """建表并在空库时写入种子数据。返回数据库路径。"""
    path = Path(db_path or default_db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=10)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(SCHEMA)
        # 对位版本初始值：新库为 v1；从旧版本升级的库按“已应用的对位更新次数”
        # 推断一个不回退的版本号，避免已有复核批次被误判为全部过期。
        db.execute(
            """
            INSERT OR IGNORE INTO app_meta(key, value)
            SELECT 'alignment_version', CAST(1 + COUNT(*) AS TEXT)
            FROM runs WHERE status = 'applied'
            """
        )
        db.commit()
        seed_if_empty(db)
    finally:
        db.close()
    return path


def get_meta(db: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = db.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row is not None else default


def set_meta(db: sqlite3.Connection, key: str, value) -> None:
    db.execute(
        """INSERT INTO app_meta(key, value) VALUES(?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (key, str(value)),
    )


def init_app(app):
    app.teardown_appcontext(close_db)

    @app.cli.command("init-db")
    def _init_db_command():  # pragma: no cover
        p = init_db(app.config["DATABASE"])
        print("initialized", p)
