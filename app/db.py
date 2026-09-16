"""SQLite 持久层：单文件本机库存储，无任何外部服务。"""
from __future__ import annotations

import sqlite3

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 能力档案：不假定能力相同，逐项登记可用手与各类开关
CREATE TABLE IF NOT EXISTS capability_profiles (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    usable_hand        TEXT,                 -- left / right / NULL（无可用手）
    foot_pedal         INTEGER NOT NULL DEFAULT 0,
    single_key_switch  INTEGER NOT NULL DEFAULT 0,
    voice_confirmation INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL
);

-- 标本册
CREATE TABLE IF NOT EXISTS volumes (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    binding_direction TEXT NOT NULL,         -- left / right / top
    created_at        TEXT NOT NULL
);

-- 册页次序：seq 即翻拍顺序
CREATE TABLE IF NOT EXISTS pages (
    id            TEXT PRIMARY KEY,
    volume_id     TEXT NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    label         TEXT NOT NULL,             -- 页码
    side          TEXT NOT NULL,             -- recto / verso（正反面）
    has_interleaf INTEGER NOT NULL DEFAULT 0,
    UNIQUE (volume_id, seq)
);

-- 易碎区：页面归一化坐标（0~1）矩形，压条不得落入
CREATE TABLE IF NOT EXISTS fragile_zones (
    id        TEXT PRIMARY KEY,
    volume_id TEXT NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
    page_id   TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    x REAL NOT NULL, y REAL NOT NULL, w REAL NOT NULL, h REAL NOT NULL,
    note      TEXT
);

-- 器材：夹具 / 压条 / 翻页垫 / 相机遥控
CREATE TABLE IF NOT EXISTS equipment (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    name       TEXT NOT NULL,
    attributes TEXT NOT NULL DEFAULT '{}',   -- JSON：遥控 trigger、压条 width 等
    created_at TEXT NOT NULL
);

-- 翻拍计划：一次编排对应一次执行
CREATE TABLE IF NOT EXISTS plans (
    id            TEXT PRIMARY KEY,
    volume_id     TEXT NOT NULL REFERENCES volumes(id),
    profile_id    TEXT NOT NULL REFERENCES capability_profiles(id),
    equipment_ids TEXT NOT NULL,             -- JSON 数组
    status        TEXT NOT NULL DEFAULT 'ready',   -- ready / active / completed
    created_at    TEXT NOT NULL
);

-- 计划步骤：编排结果，也是执行进度的载体
CREATE TABLE IF NOT EXISTS steps (
    id            TEXT PRIMARY KEY,
    plan_id       TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    kind          TEXT NOT NULL,             -- secure/lift_interleaf/flatten/pre_shoot_check/shoot/post_shoot_check/reset
    page_id       TEXT,
    page_seq      INTEGER,
    page_label    TEXT,
    side          TEXT,
    instruction   TEXT NOT NULL,             -- 面向操作者的逐步指令
    channels      TEXT NOT NULL DEFAULT '[]',-- JSON：本步占用的手/开关通道
    placement     TEXT,                      -- JSON：压条落位（避开易碎区）
    support_state TEXT NOT NULL DEFAULT '{}',-- JSON：本步结束后的支撑位占用
    checkpoint    INTEGER NOT NULL DEFAULT 0,-- 安全落点
    depends_on    TEXT NOT NULL DEFAULT '[]',-- JSON：本步依赖的资料 id（过期判定用）
    input_hash    TEXT NOT NULL,             -- 本步输入指纹
    status        TEXT NOT NULL DEFAULT 'pending', -- pending / done / stale
    UNIQUE (plan_id, seq)
);

-- 执行会话
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    plan_id      TEXT NOT NULL REFERENCES plans(id),
    status       TEXT NOT NULL DEFAULT 'active',     -- active / completed
    created_at   TEXT NOT NULL,
    completed_at TEXT,
    frozen_path  TEXT,                         -- 完成版冻结的实际路径（JSON）
    input_hash   TEXT                          -- 完成版冻结的输入哈希
);

-- 事件流：确认 / 撤回 / 核对失败 / 异常 / 重排，全部入档
CREATE TABLE IF NOT EXISTS events (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    type            TEXT NOT NULL,
    source          TEXT,                      -- single_key / foot_pedal / voice / hand
    step_id         TEXT,
    step_kind       TEXT,
    page_label      TEXT,
    payload         TEXT,
    result          TEXT,                      -- 幂等重放用：当时的响应快照
    client_event_id TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (session_id, seq)
);

-- 开关抖动去重：同一 client_event_id 只生效一次
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_client_idem
    ON events (session_id, client_event_id) WHERE client_event_id IS NOT NULL;
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
