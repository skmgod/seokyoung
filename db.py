"""데이터베이스 연결과 스키마."""
import os
import sqlite3

from flask import g, current_app

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    pw_hash TEXT NOT NULL,
    name TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- 빌라(건물)
CREATE TABLE IF NOT EXISTS buildings (
    id INTEGER PRIMARY KEY,
    code TEXT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '빌라',
    address TEXT,
    zip TEXT,
    owner_name TEXT,
    owner_phone TEXT,
    manager TEXT,
    elec_no TEXT,
    water_no TEXT,
    gas_no TEXT,
    -- 공과금 부과 방식: none(부과안함) / price(사용량x단가) / ratio(사용량 비율 배분) / equal(세대 균등)
    elec_method TEXT NOT NULL DEFAULT 'none',
    water_method TEXT NOT NULL DEFAULT 'ratio',
    gas_method TEXT NOT NULL DEFAULT 'none',
    common_method TEXT NOT NULL DEFAULT 'none',
    bill_rent INTEGER NOT NULL DEFAULT 0,
    memo TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created TEXT DEFAULT (datetime('now','localtime'))
);

-- 세대(호수)
CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY,
    building_id INTEGER NOT NULL REFERENCES buildings(id) ON DELETE CASCADE,
    ho TEXT NOT NULL,
    tenant TEXT,
    phone TEXT,
    mobile TEXT,
    move_in TEXT,
    move_out TEXT,
    status TEXT NOT NULL DEFAULT '입주',
    deposit INTEGER NOT NULL DEFAULT 0,
    rent INTEGER NOT NULL DEFAULT 0,
    auto_transfer INTEGER NOT NULL DEFAULT 0,
    memo TEXT,
    UNIQUE (building_id, ho)
);

-- 관리비 항목 마스터
CREATE TABLE IF NOT EXISTS fee_items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    sort INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

-- 빌라별 항목 금액. method: unit(세대당 금액) / split(총액을 입주세대로 분할)
CREATE TABLE IF NOT EXISTS building_fees (
    building_id INTEGER NOT NULL REFERENCES buildings(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES fee_items(id) ON DELETE CASCADE,
    amount INTEGER NOT NULL DEFAULT 0,
    method TEXT NOT NULL DEFAULT 'unit',
    PRIMARY KEY (building_id, item_id)
);

-- 세대별 항목 금액(빌라 기본값 대신 사용)
CREATE TABLE IF NOT EXISTS unit_fees (
    unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES fee_items(id) ON DELETE CASCADE,
    amount INTEGER NOT NULL,
    PRIMARY KEY (unit_id, item_id)
);

-- 검침 (util: elec/water/gas)
CREATE TABLE IF NOT EXISTS readings (
    unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    ym TEXT NOT NULL,
    util TEXT NOT NULL,
    prev REAL,
    curr REAL,
    PRIMARY KEY (unit_id, ym, util)
);

-- 빌라 전체 고지금액 (한전/수도사업소/가스회사) 및 단가 (util: elec/water/gas/common)
CREATE TABLE IF NOT EXISTS building_bills (
    building_id INTEGER NOT NULL REFERENCES buildings(id) ON DELETE CASCADE,
    ym TEXT NOT NULL,
    util TEXT NOT NULL,
    amount INTEGER NOT NULL DEFAULT 0,
    unit_price REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (building_id, ym, util)
);

-- 고지(부과) 내역. kind: normal(월 관리비) / carry(이월 미납)
CREATE TABLE IF NOT EXISTS bills (
    id INTEGER PRIMARY KEY,
    unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    ym TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'normal',
    amount INTEGER NOT NULL,
    late_fee INTEGER NOT NULL DEFAULT 0,
    due_date TEXT NOT NULL,
    issued TEXT,
    memo TEXT,
    UNIQUE (unit_id, ym, kind)
);

CREATE TABLE IF NOT EXISTS bill_lines (
    id INTEGER PRIMARY KEY,
    bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    amount INTEGER NOT NULL,
    detail TEXT,
    sort INTEGER NOT NULL DEFAULT 0
);

-- 수납
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    pay_date TEXT NOT NULL,
    amount INTEGER NOT NULL,
    method TEXT,
    payer TEXT,
    memo TEXT,
    created TEXT DEFAULT (datetime('now','localtime'))
);

-- 수납액을 고지 건에 배정한 내역 (오래된 고지부터 자동 배정)
CREATE TABLE IF NOT EXISTS allocations (
    payment_id INTEGER NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
    bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    amount INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_units_building ON units(building_id);
CREATE INDEX IF NOT EXISTS idx_bills_unit ON bills(unit_id, ym);
CREATE INDEX IF NOT EXISTS idx_payments_unit ON payments(unit_id, pay_date);
CREATE INDEX IF NOT EXISTS idx_alloc_bill ON allocations(bill_id);
CREATE INDEX IF NOT EXISTS idx_alloc_payment ON allocations(payment_id);
"""

DEFAULT_SETTINGS = {
    "company_name": "",
    "company_phone": "",
    "company_address": "",
    "bank_info": "",
    "late_rate": "2",
    "round_unit": "10",
    "round_mode": "floor",
    "due_offset": "1",
    "due_day": "25",
    "notice": "납부마감일까지 납부해 주시기 바랍니다. 입금 시 입금자명을 '빌라명+호수'로 적어 주세요.",
    "dunning_text": "귀댁의 관리비가 아래와 같이 체납되어 있습니다. 체납이 계속되면 제반 업무 수행에 지장이 있으니 기일 내 납부해 주시기 바랍니다.",
}

DEFAULT_FEE_ITEMS = ["일반관리비", "청소비", "수선유지비", "승강기유지비", "정화조", "인터넷", "유선방송", "기타"]


def db_path():
    return os.path.join(current_app.instance_path, "housing.db")


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(db_path())
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(path):
    conn = connect(path)
    conn.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    if conn.execute("SELECT COUNT(*) FROM fee_items").fetchone()[0] == 0:
        for i, name in enumerate(DEFAULT_FEE_ITEMS):
            conn.execute("INSERT INTO fee_items(name, sort) VALUES (?, ?)", (name, i))
    conn.commit()
    conn.close()


def get_settings(conn):
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
