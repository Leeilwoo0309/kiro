"""
Local_Store: 로컬 SQLite 저장소 (requirements 9.5)

- 파일은 data/ 아래에만 둔다. data/는 .gitignore에 등록되어 있다 (R1.2).
- 쓰기는 BEGIN IMMEDIATE 트랜잭션으로 직렬화한다. 단일 사용자 로컬 실행에서
  기획 명세의 "사용자 단위 잠금"과 같은 효과를 낸다 (R10.1).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pledges (
    month                TEXT PRIMARY KEY,          -- YYYY-MM (Asia/Seoul)
    income_krw           INTEGER,
    total_budget_krw     INTEGER,
    category_budgets     TEXT NOT NULL DEFAULT '{}', -- {"food": 100000}
    adjust_categories    TEXT NOT NULL DEFAULT '[]',
    protected_categories TEXT NOT NULL DEFAULT '[]',
    version              INTEGER NOT NULL DEFAULT 1,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS imports (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,       -- csv | pdf
    file_hash  TEXT NOT NULL,
    row_counts TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 원본 거래. 날짜·금액·거래처는 덮어쓰지 않는다.
CREATE TABLE IF NOT EXISTS transactions (
    id            TEXT PRIMARY KEY,
    import_id     TEXT REFERENCES imports(id),
    date          TEXT NOT NULL,    -- YYYY-MM-DD (Asia/Seoul)
    time          TEXT,             -- HH:MM (명세서에 있으면)
    amount_krw    INTEGER NOT NULL CHECK (amount_krw > 0),
    direction     TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    payment_type  TEXT NOT NULL CHECK (payment_type IN ('credit', 'debit', 'transfer')),
    merchant_name TEXT NOT NULL,
    account_ref   TEXT,
    source_id     TEXT
);
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);

-- 거래별 현재 해석
CREATE TABLE IF NOT EXISTS interpretations (
    transaction_id        TEXT PRIMARY KEY REFERENCES transactions(id) ON DELETE CASCADE,
    category              TEXT,     -- out 거래만. 7개 코드
    economic_type         TEXT NOT NULL,
    classification_source TEXT NOT NULL CHECK (classification_source IN ('rule', 'model', 'user')),
    review_status         TEXT NOT NULL CHECK (review_status IN ('pending', 'confirmed')),
    economic_status       TEXT NOT NULL DEFAULT 'confirmed' CHECK (economic_status IN ('pending', 'confirmed')),
    flags                 TEXT NOT NULL DEFAULT '[]'
);

-- 입금과 지출의 연결
CREATE TABLE IF NOT EXISTS allocations (
    id          TEXT PRIMARY KEY,
    credit_id   TEXT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    expense_id  TEXT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('settlement', 'refund')),
    amount_krw  INTEGER NOT NULL CHECK (amount_krw > 0),
    state       TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'reverted')),
    proposal_id TEXT,
    created_at  TEXT NOT NULL
);
-- 같은 입금·지출 쌍의 중복 연결 금지 (R6.4)
CREATE UNIQUE INDEX IF NOT EXISTS uq_alloc_pair ON allocations(credit_id, expense_id) WHERE state = 'active';

-- 미수 정산 (사용자 진술만 있고 입금이 확인되지 않은 금액)
CREATE TABLE IF NOT EXISTS receivables (
    id          TEXT PRIMARY KEY,
    expense_id  TEXT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    amount_krw  INTEGER NOT NULL CHECK (amount_krw > 0),
    note        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_sessions (
    id             TEXT PRIMARY KEY,
    month          TEXT NOT NULL,
    ledger_version INTEGER NOT NULL,
    plan_version   INTEGER,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_issues (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL REFERENCES audit_sessions(id) ON DELETE CASCADE,
    rule          TEXT NOT NULL,
    category      TEXT,
    expense_id    TEXT,
    commitment_id TEXT,
    evidence_ids  TEXT NOT NULL DEFAULT '[]',
    candidate_ids TEXT NOT NULL DEFAULT '[]',
    reason        TEXT NOT NULL,
    question      TEXT NOT NULL,
    question_source TEXT NOT NULL DEFAULT 'template',
    status        TEXT NOT NULL,
    turns         INTEGER NOT NULL DEFAULT 0,
    context_turns TEXT NOT NULL DEFAULT '[]',
    notes         TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS proposals (
    id             TEXT PRIMARY KEY,
    issue_id       TEXT NOT NULL REFERENCES audit_issues(id) ON DELETE CASCADE,
    explanation    TEXT,
    patch          TEXT NOT NULL,
    preview        TEXT NOT NULL DEFAULT '{}',
    ledger_version INTEGER NOT NULL,
    status         TEXT NOT NULL,   -- proposal_ready | applied | rejected | stale
    result         TEXT,
    created_at     TEXT NOT NULL
);

-- 변경 기록 (R10.5)
CREATE TABLE IF NOT EXISTS events (
    id         TEXT PRIMARY KEY,
    entity     TEXT NOT NULL,
    entity_id  TEXT,
    before     TEXT,
    after      TEXT,
    reason     TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    month      TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    version    INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

-- 전수 감사(청문회): 한 달의 모든 지출을 거래처별로 묶어 하나씩 따진다
CREATE TABLE IF NOT EXISTS hearings (
    id             TEXT PRIMARY KEY,
    month          TEXT NOT NULL,
    ledger_version INTEGER NOT NULL,
    tone           TEXT NOT NULL,
    ai_used        INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hearing_items (
    id            TEXT PRIMARY KEY,
    hearing_id    TEXT NOT NULL REFERENCES hearings(id) ON DELETE CASCADE,
    position      INTEGER NOT NULL,
    merchant_name TEXT NOT NULL,
    category      TEXT NOT NULL,
    tx_ids        TEXT NOT NULL,          -- 묶인 거래 ID 목록
    count         INTEGER NOT NULL,
    total_krw     INTEGER NOT NULL,       -- 본인 부담 합계 (서버 계산)
    jab           TEXT NOT NULL,          -- 딴지 문장
    jab_source    TEXT NOT NULL,          -- model | template
    suggestion    TEXT NOT NULL,          -- keep | reduce | cut
    suggested_krw INTEGER NOT NULL,       -- 제안 목표 금액 (서버 계산)
    reason        TEXT,
    response      TEXT,                   -- keep | reduce | cut | necessary (사용자 답)
    target_krw    INTEGER,
    note          TEXT
);

-- 청문회 안건: 코드가 찾은 이상한 소비 패턴 하나와 그에 대한 AI와의 대화
CREATE TABLE IF NOT EXISTS hearing_cases (
    id             TEXT PRIMARY KEY,
    hearing_id     TEXT NOT NULL REFERENCES hearings(id) ON DELETE CASCADE,
    position       INTEGER NOT NULL,
    kind           TEXT NOT NULL,     -- repeat_amount | repeat_merchant | late_night | spike | burst
    title          TEXT NOT NULL,
    category       TEXT NOT NULL,
    tx_ids         TEXT NOT NULL,
    count          INTEGER NOT NULL,
    total_krw      INTEGER NOT NULL,
    avg_krw        INTEGER NOT NULL,
    facts          TEXT NOT NULL,     -- 거래처, 시각 등 근거 요약 (JSON)
    label          TEXT,              -- 사용자가 밝힌 정체 (예: 핫식스)
    status         TEXT NOT NULL,     -- open | proposed | agreed | kept
    proposal_count INTEGER,
    proposal_krw   INTEGER,
    target_krw     INTEGER,
    reason         TEXT,
    turns          TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS commitments (
    id          TEXT PRIMARY KEY,
    issue_id    TEXT,
    action      TEXT NOT NULL,
    metric      TEXT NOT NULL,
    due_at      TEXT NOT NULL,      -- YYYY-MM-DD
    status      TEXT NOT NULL DEFAULT 'pending', -- pending | done | partial | missed
    result_note TEXT,
    created_at  TEXT NOT NULL
);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "ledger_version": 0,
    "tone": "calm",                 # calm | hearing (국감 모드)
    "presentation_mode": False,     # 발표 모드 (R1.10)
    "owner_names": [],              # 본인 이체 판별용 이름 (R5.2)
    "shared_window_days": 7,        # 공동 결제 후보 탐색 기간 (R8.5)
    "min_shared_krw": 10_000,       # 공동 결제 안건 최소 금액
}

# 데이터 삭제 시 비울 테이블 (자식 → 부모 순서)
USER_TABLES = [
    "hearing_cases", "hearing_items", "hearings", "proposals", "audit_issues", "audit_sessions", "allocations", "receivables",
    "interpretations", "transactions", "imports", "events", "plans", "commitments", "pledges",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value: Optional[str], default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def _connect() -> sqlite3.Connection:
    path = config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        # 이전 버전 DB 보정: 시각 열 추가
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
        if "time" not in cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN time TEXT")
        # 기존 청문회 기록은 보존하고 새 기간 메타데이터만 추가한다.
        hearing_cols = {r["name"] for r in conn.execute("PRAGMA table_info(hearings)")}
        for name, definition in (("start_date", "TEXT"), ("end_date", "TEXT"),
                                 ("context", "TEXT NOT NULL DEFAULT '{}'")):
            if name not in hearing_cols:
                conn.execute(f"ALTER TABLE hearings ADD COLUMN {name} {definition}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hearing_period ON hearings(start_date, end_date, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hearing_cases ON hearing_cases(hearing_id, position)")
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (key, dumps(value))
            )
    finally:
        conn.close()


@contextmanager
def read() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """쓰기 트랜잭션. 예외가 나면 전체를 되돌린다."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


def get_setting(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return DEFAULT_SETTINGS.get(key)
    return loads(row["value"])


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, dumps(value)),
    )


def ledger_version(conn: sqlite3.Connection) -> int:
    return int(get_setting(conn, "ledger_version") or 0)


def bump_ledger_version(conn: sqlite3.Connection) -> int:
    version = ledger_version(conn) + 1
    set_setting(conn, "ledger_version", version)
    return version


def add_event(
    conn: sqlite3.Connection,
    entity: str,
    entity_id: Optional[str],
    before: Any,
    after: Any,
    reason: str,
) -> str:
    event_id = new_id("event")
    conn.execute(
        "INSERT INTO events(id, entity, entity_id, before, after, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event_id, entity, entity_id, dumps(before), dumps(after), reason, now_iso()),
    )
    return event_id


def delete_all_user_data() -> None:
    """로컬 데이터 전체 삭제 (R1.9). 설정 중 원장 버전만 0으로 되돌린다."""
    with tx() as conn:
        for table in USER_TABLES:
            conn.execute(f"DELETE FROM {table}")  # 테이블 이름은 위 상수 목록에서만 온다
        set_setting(conn, "ledger_version", 0)
        set_setting(conn, "owner_names", [])
    conn = _connect()
    try:
        conn.execute("VACUUM")  # 삭제한 페이지를 파일에서도 정리
    finally:
        conn.close()
