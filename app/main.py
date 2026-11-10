"""
FastAPI 애플리케이션 엔트리포인트 (MVP, requirements 9.4)

실행: python -m app   (127.0.0.1에서만 수신, R1.1)

기존 엔드포인트:
- POST /upload/pdf            : PDF 업로드 → 파싱 → 전처리 → CSV 반환
- POST /upload/pdf/download   : 변환 CSV 다운로드
- GET  /health                : 헬스체크
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import ai_adapter, audit, config, forecast, hearing, importer, ledger, store
from app.classifier import CATEGORIES, CATEGORY_LABELS
from app.config import MAX_AMOUNT_KRW, MAX_PDF_BYTES
from app.errors import AppError, bad_request, not_found
from app.pdf_parser import PDFParseError
from app.pipeline import PipelineResult, run_pdf_pipeline

log = logging.getLogger("app")
STATIC_DIR = Path(__file__).resolve().parent / "static"

# 허용 파일 크기 (FastAPI 레이어에서도 이중 검사)
MAX_UPLOAD_BYTES = MAX_PDF_BYTES  # 20 MB

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost", "testserver"}  # testserver는 FastAPI TestClient


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    store.init_db()
    if not ai_adapter.is_configured():
        # 키 값은 출력하지 않는다 (R11.5)
        log.warning("AI 키 또는 모델이 없어 AI 기능을 끄고 수동 경로만 제공합니다. 설정 화면에서 입력할 수 있습니다.")
    yield


app = FastAPI(
    title="AI 가계부 API",
    description="국정감사형 소비 점검 MVP (로컬 단일 사용자)",
    version="0.2.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def local_only(request: Request, call_next):
    """로컬 접속만 허용한다 (R1.1). 외부 터널로 노출돼도 개인 데이터를 내주지 않는다."""
    host = request.client.host if request.client else ""
    if host not in _LOOPBACK:
        return JSONResponse(status_code=403, content={"error": {"code": "forbidden",
                                                                "message": "로컬에서만 사용할 수 있습니다."}})
    # 다른 웹사이트가 브라우저를 통해 이 서버를 조작하지 못하게 막는다 (DNS 리바인딩, CSRF)
    host_header = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    if host_header and host_header not in _ALLOWED_HOSTS:
        return JSONResponse(status_code=403, content={"error": {"code": "forbidden", "message": "허용되지 않은 Host입니다."}})
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
        origin_host = origin.split("://", 1)[-1].rsplit(":", 1)[0].strip("[]")
        if origin_host not in _ALLOWED_HOSTS:
            return JSONResponse(status_code=403, content={"error": {"code": "forbidden", "message": "다른 사이트에서 온 요청입니다."}})
    response = await call_next(request)
    if not request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(AppError)
async def app_error_handler(_: Request, exc: AppError):
    return JSONResponse(status_code=exc.status,
                        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}})


# ───────────────────────── 기존 엔드포인트 ─────────────────────────

@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.post("/upload/pdf")
async def upload_pdf(file: UploadFile = File(...)):
    """
    PDF 거래 내역 파일을 업로드합니다.

    - 텍스트 기반 PDF만 허용 (스캔 이미지 PDF 거부)
    - 최대 파일 크기: 20MB
    - 응답: 전처리 완료된 CSV 문자열 + 분석 요약
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="PDF 파일(.pdf)만 업로드할 수 있습니다.")

    file_bytes = await file.read()

    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"파일 크기가 20MB를 초과합니다. 현재 크기: {len(file_bytes) / 1024 / 1024:.1f}MB",
        )

    try:
        result: PipelineResult = run_pdf_pipeline(file_bytes)
    except PDFParseError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return JSONResponse(
        content={
            "summary": {
                "total_transactions": result.total_transactions,
                "skipped_rows": result.skipped_rows,
                "duplicate_candidates": len(result.duplicate_indices),
                "warnings": result.warnings,
            },
            "csv": result.csv_string,
        }
    )


@app.post("/upload/pdf/download")
async def upload_pdf_download(file: UploadFile = File(...)):
    """PDF를 업로드하면 전처리 완료된 CSV 파일을 직접 다운로드합니다. (엑셀 호환 UTF-8 BOM 포함)"""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="PDF 파일(.pdf)만 업로드할 수 있습니다.")

    file_bytes = await file.read()
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="파일 크기가 20MB를 초과합니다.")

    try:
        result: PipelineResult = run_pdf_pipeline(file_bytes)
    except PDFParseError as e:
        raise HTTPException(status_code=422, detail=str(e))

    original_name = file.filename.replace(".pdf", "")
    return Response(
        content=result.csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{original_name}_processed.csv"'},
    )


# ───────────────────────── 화면 ─────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


# ───────────────────────── 설정, 상태 ─────────────────────────

class SettingsPatch(BaseModel):
    tone: Optional[str] = None
    presentation_mode: Optional[bool] = None
    owner_names: Optional[list[str]] = None
    shared_window_days: Optional[int] = Field(default=None, ge=1, le=31)
    min_shared_krw: Optional[int] = Field(default=None, ge=0, le=MAX_AMOUNT_KRW)


def _settings(conn) -> dict:
    return {k: store.get_setting(conn, k) for k in
            ("tone", "presentation_mode", "owner_names", "shared_window_days", "min_shared_krw", "ledger_version")}


@app.get("/settings")
def get_settings():
    with store.read() as conn:
        return {**_settings(conn), "ai": ai_adapter.status(),
                "categories": [{"code": c, "label": CATEGORY_LABELS[c]} for c in CATEGORIES]}


@app.patch("/settings")
def patch_settings(body: SettingsPatch):
    if body.tone is not None and body.tone not in ("calm", "hearing"):
        raise bad_request("tone은 calm 또는 hearing이어야 합니다.")
    with store.tx() as conn:
        for key, value in body.model_dump(exclude_none=True).items():
            if key == "owner_names":
                value = [v.strip() for v in value if v and v.strip()][:10]
            store.set_setting(conn, key, value)
        return _settings(conn)


@app.get("/ai/status")
def ai_status():
    return ai_adapter.status()


_AI_ERRORS = {
    "auth": "키가 올바르지 않거나 다른 BAZE 배포에서 발급된 키입니다.",
    "connection": "BAZE 서버에 연결할 수 없습니다. 네트워크를 확인해 주세요.",
    "not_configured": "API 키를 먼저 입력해 주세요.",
}


@app.get("/ai/models")
def ai_models():
    try:
        return {"models": ai_adapter.list_models(), "current": config.llm_model() or None}
    except ai_adapter.AIUnavailable as e:
        raise bad_request(_AI_ERRORS.get(e.reason, f"모델 목록을 가져오지 못했습니다 ({e.reason})."))


class AIConfigBody(BaseModel):
    api_key: Optional[str] = Field(default=None, max_length=300)
    model: Optional[str] = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9._/:\-]+$")
    clear_key: bool = False


@app.put("/ai/config")
def ai_config(body: AIConfigBody):
    """웹에서 입력한 키·모델을 data/ai_settings.json(권한 600)에 저장한다. 키는 응답·로그에 남기지 않는다."""
    current = config.load_ai_settings()
    if body.clear_key:
        current.pop("api_key", None)
    if body.api_key is not None and body.api_key.strip():
        key = body.api_key.strip()
        try:
            models = ai_adapter.list_models(key)  # 저장 전에 키가 맞는지 확인
        except ai_adapter.AIUnavailable as e:
            raise bad_request(_AI_ERRORS.get(e.reason, f"키를 확인하지 못했습니다 ({e.reason})."))
        current["api_key"] = key
        if not (body.model or current.get("model") or config.llm_model()) and models:
            current["model"] = "gpt-6-luna" if any(m["id"] == "gpt-6-luna" for m in models) else models[0]["id"]
    if body.model:
        current["model"] = body.model
    config.save_ai_settings(current)
    return ai_adapter.status()


# ───────────────────────── 소비 공약 (R2) ─────────────────────────

class PledgeBody(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    income_krw: Optional[int] = Field(default=None, ge=0, le=MAX_AMOUNT_KRW)
    total_budget_krw: Optional[int] = Field(default=None, ge=0, le=MAX_AMOUNT_KRW)
    category_budgets: dict[str, int] = Field(default_factory=dict)
    adjust_categories: list[str] = Field(default_factory=list)
    protected_categories: list[str] = Field(default_factory=list)


@app.get("/pledge")
def get_pledge(month: Optional[str] = None):
    with store.read() as conn:
        if month is None:
            month = ledger.summarize(conn)["month"]
        return {"month": month, "pledge": ledger.get_pledge(conn, month)}


@app.put("/pledge")
def put_pledge(body: PledgeBody):
    for code, amount in body.category_budgets.items():
        if code not in CATEGORIES:
            raise bad_request(f"알 수 없는 카테고리: {code}", {"allowed": CATEGORIES})
        if not 0 <= amount <= MAX_AMOUNT_KRW:
            raise bad_request("금액은 0원 이상 999,999,999원 이하만 입력할 수 있습니다.")
    for code in body.adjust_categories + body.protected_categories:
        if code not in CATEGORIES:
            raise bad_request(f"알 수 없는 카테고리: {code}", {"allowed": CATEGORIES})
    with store.tx() as conn:
        prev = ledger.get_pledge(conn, body.month)
        version = (prev["version"] + 1) if prev else 1
        conn.execute(
            "INSERT INTO pledges(month, income_krw, total_budget_krw, category_budgets, adjust_categories, "
            "protected_categories, version, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(month) DO UPDATE SET income_krw = excluded.income_krw, "
            "total_budget_krw = excluded.total_budget_krw, category_budgets = excluded.category_budgets, "
            "adjust_categories = excluded.adjust_categories, protected_categories = excluded.protected_categories, "
            "version = excluded.version, updated_at = excluded.updated_at",
            (body.month, body.income_krw, body.total_budget_krw, store.dumps(body.category_budgets),
             store.dumps(body.adjust_categories), store.dumps(body.protected_categories), version, store.now_iso()),
        )
        store.add_event(conn, "pledge", body.month, None, {"version": version}, "공약 저장")
        return {"month": body.month, "pledge": ledger.get_pledge(conn, body.month)}


# ───────────────────────── 가져오기 (R3) ─────────────────────────

@app.post("/imports/preview")
async def imports_preview(file: UploadFile = File(...), password: Optional[str] = Form(None)):
    """password: 암호가 걸린 은행 PDF용. 저장·기록하지 않는다."""
    data = await file.read()
    return importer.preview(file.filename or "", data, password or None)


class CommitBody(BaseModel):
    exclude_idx: list[int] = Field(default_factory=list)
    use_ai: bool = True


@app.post("/imports/{preview_id}/commit")
def imports_commit(preview_id: str, body: CommitBody):
    return importer.commit(preview_id, body.exclude_idx, body.use_ai)


# ───────────────────────── 대시보드, 거래 (R4, R5, R7) ─────────────────────────

@app.get("/dashboard")
def dashboard(month: Optional[str] = None):
    with store.read() as conn:
        summary = ledger.summarize(conn, month)
        issues = conn.execute(
            "SELECT i.id, i.rule, i.reason, i.question, i.status, i.session_id FROM audit_issues i "
            "JOIN audit_sessions s ON s.id = i.session_id WHERE s.month = ? "
            "ORDER BY s.created_at DESC, i.rowid LIMIT 3",
            (summary["month"],),
        ).fetchall() if summary["month"] else []
        summary["recent_issues"] = [dict(r) for r in issues]
        return summary


@app.get("/transactions")
def list_transactions(month: Optional[str] = None):
    with store.read() as conn:
        rows = ledger.load_rows(conn)
        by_expense, by_credit = ledger.allocation_sums(ledger.active_allocations(conn))
        if month:
            rows = [r for r in rows if ledger.month_of(r["date"]) == month]
        for r in rows:
            used = by_expense.get(r["id"], 0) if r["direction"] == "out" else by_credit.get(r["id"], 0)
            r["linked_krw"] = used
            r["needs_review"] = ledger.needs_review(r)
        return {"transactions": rows, "ledger_version": store.ledger_version(conn)}


class CategoryBody(BaseModel):
    category: str


@app.patch("/transactions/{tx_id}/category")
def set_category(tx_id: str, body: CategoryBody):
    """사용자 직접 수정 (R4.7). 금액은 바뀌지 않고 분류만 바뀐다."""
    if body.category not in CATEGORIES:
        raise bad_request("카테고리는 7개 코드 중 하나여야 합니다.", {"allowed": CATEGORIES})
    with store.tx() as conn:
        row = ledger.get_row(conn, tx_id)
        if row["direction"] != "out":
            raise bad_request("지출 거래만 카테고리를 바꿀 수 있습니다.")
        conn.execute("UPDATE interpretations SET category = ?, classification_source = 'user', "
                     "review_status = 'confirmed' WHERE transaction_id = ?", (body.category, tx_id))
        store.add_event(conn, "interpretation", tx_id, {"category": row["category"]},
                        {"category": body.category}, "사용자 분류 수정")
        return {"transaction_id": tx_id, "category": body.category,
                "ledger_version": store.bump_ledger_version(conn)}


class EconomicBody(BaseModel):
    economic_type: str


@app.patch("/transactions/{tx_id}/economic-type")
def set_economic_type(tx_id: str, body: EconomicBody):
    """거래 성격 확인 (카드대금, 본인 이체 등). 사용자가 직접 확인한 값으로 바꾼다."""
    if body.economic_type not in ledger.ECONOMIC_TYPES:
        raise bad_request("알 수 없는 거래 성격입니다.", {"allowed": ledger.ECONOMIC_TYPES})
    with store.tx() as conn:
        row = ledger.get_row(conn, tx_id)
        out_types = {"expense", "card_settlement", "own_transfer", "unknown"}
        in_types = {"income", "reimbursement", "refund", "own_transfer", "unknown"}
        allowed = out_types if row["direction"] == "out" else in_types
        if body.economic_type not in allowed:
            raise bad_request("이 거래 방향에 쓸 수 없는 성격입니다.", {"allowed": sorted(allowed)})
        linked = conn.execute(
            "SELECT 1 FROM allocations WHERE (expense_id = ? OR credit_id = ?) AND state = 'active'", (tx_id, tx_id)
        ).fetchone()
        if linked and body.economic_type != row["economic_type"]:
            raise bad_request("정산·환불이 연결된 거래는 성격을 바꿀 수 없습니다.")
        conn.execute("UPDATE interpretations SET economic_type = ?, economic_status = 'confirmed', "
                     "category = COALESCE(category, CASE WHEN ? = 'out' THEN 'other' END) "
                     "WHERE transaction_id = ?", (body.economic_type, row["direction"], tx_id))
        store.add_event(conn, "interpretation", tx_id, {"economic_type": row["economic_type"]},
                        {"economic_type": body.economic_type}, "사용자 거래 성격 확인")
        return {"transaction_id": tx_id, "economic_type": body.economic_type,
                "ledger_version": store.bump_ledger_version(conn)}


# ───────────────────────── 감사 (R8~R10) ─────────────────────────

class AuditBody(BaseModel):
    month: Optional[str] = None
    use_ai: bool = True


@app.post("/audits")
def create_audit(body: AuditBody):
    return audit.create_session(body.month, body.use_ai)


@app.get("/audits/{session_id}")
def get_audit(session_id: str):
    return audit.get_session(session_id)


@app.post("/issues/{issue_id}/explanations")
def explain(issue_id: str, body: dict = Body(...)):
    return audit.explain(issue_id, body)


@app.post("/issues/{issue_id}/resolve")
def resolve(issue_id: str):
    return audit.resolve_issue(issue_id)


class ConfirmBody(BaseModel):
    ledger_version: int


@app.post("/proposals/{proposal_id}/confirm")
def confirm(proposal_id: str, body: ConfirmBody):
    result = audit.confirm(proposal_id, body.ledger_version)
    if result.get("__conflict__"):
        result.pop("__conflict__")
        return JSONResponse(status_code=409, content={"error": {
            "code": "conflict", "message": result["message"], "details": result}})
    return result


@app.post("/proposals/{proposal_id}/reject")
def reject(proposal_id: str):
    return audit.reject(proposal_id)


# ───────────────────────── 전수 감사 (청문회) ─────────────────────────

class HearingBody(BaseModel):
    month: Optional[str] = None
    use_ai: bool = True
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    days: int = Field(default=30, ge=7, le=30)


@app.post("/hearings")
def create_hearing(body: HearingBody):
    return hearing.create(**body.model_dump())


@app.get("/hearing-periods")
def hearing_periods():
    return hearing.period_options()


@app.get("/hearings/latest")
def latest_hearing(month: Optional[str] = None, start_date: Optional[str] = None,
                   end_date: Optional[str] = None, days: int = 30):
    return {"hearing": hearing.latest(month, start_date, end_date, days)}


@app.get("/hearings/{hearing_id}")
def get_hearing(hearing_id: str):
    return hearing.get(hearing_id)


class CaseReply(BaseModel):
    text: Optional[str] = Field(default=None, max_length=300)
    choice: Optional[str] = None  # keep | one_off | adjust | accept | reject
    target_count: Optional[int] = Field(default=None, ge=0, strict=True)


@app.post("/cases/{case_id}/open")
def open_hearing_case(case_id: str):
    return hearing.open_case(case_id)


@app.post("/cases/{case_id}/reply")
def reply_case(case_id: str, body: CaseReply):
    return hearing.reply(case_id, body.text, body.choice, body.target_count)


@app.get("/plan/adjusted")
def adjusted_plan():
    return {"hearing": hearing.latest_adjusted()}


@app.get("/plan/next")
def next_plan():
    return {"plan": hearing.next_month_plan()}


@app.post("/hearings/{hearing_id}/apply")
def apply_hearing(hearing_id: str):
    return hearing.apply(hearing_id)


# ───────────────────────── 계획 (R13) ─────────────────────────

@app.post("/plans/preview")
def plans_preview(body: dict = Body(...)):
    with store.read() as conn:
        return forecast.preview(conn, body.get("base_month"), body)


@app.get("/plans/{month}")
def get_plan(month: str):
    with store.read() as conn:
        row = conn.execute("SELECT * FROM plans WHERE month = ?", (month,)).fetchone()
        if row is None:
            return {"month": month, "plan": None}
        return {"month": month, "plan": store.loads(row["data"], {}), "version": row["version"]}


@app.put("/plans/{month}")
def put_plan(month: str, body: dict = Body(...)):
    with store.tx() as conn:
        # 계획 저장만으로 실제 저축액을 늘리지 않는다 (R13.12). 사용자가 입력한 값을 그대로 둔다.
        return forecast.save_plan(conn, month, body)


# ───────────────────────── 후속 행동 (R14) ─────────────────────────

class CommitmentBody(BaseModel):
    action: str = Field(min_length=1, max_length=200)
    metric: str = Field(min_length=1, max_length=200)
    due_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    issue_id: Optional[str] = None


@app.post("/commitments")
def create_commitment(body: CommitmentBody):
    with store.tx() as conn:
        cid = store.new_id("commit")
        conn.execute("INSERT INTO commitments(id, issue_id, action, metric, due_at, status, created_at) "
                     "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                     (cid, body.issue_id, body.action, body.metric, body.due_at, store.now_iso()))
        if body.issue_id:
            conn.execute("UPDATE audit_issues SET status = 'resolved' WHERE id = ? "
                         "AND status IN ('applied', 'deferred', 'open', 'awaiting_context')", (body.issue_id,))
        store.add_event(conn, "commitment", cid, None, body.model_dump(), "후속 행동 저장")
        return {"commitment_id": cid}


@app.get("/commitments")
def list_commitments():
    with store.read() as conn:
        return {"commitments": [dict(r) for r in conn.execute(
            "SELECT * FROM commitments ORDER BY due_at DESC")]}


class CommitmentPatch(BaseModel):
    status: str
    result_note: Optional[str] = Field(default=None, max_length=500)


@app.patch("/commitments/{commitment_id}")
def update_commitment(commitment_id: str, body: CommitmentPatch):
    if body.status not in ("done", "partial", "missed"):
        raise bad_request("status는 done, partial, missed 중 하나여야 합니다.")
    with store.tx() as conn:
        row = conn.execute("SELECT * FROM commitments WHERE id = ?", (commitment_id,)).fetchone()
        if row is None:
            raise not_found("후속 행동을 찾을 수 없습니다.")
        conn.execute("UPDATE commitments SET status = ?, result_note = ? WHERE id = ?",
                     (body.status, body.result_note, commitment_id))
        conn.execute("UPDATE audit_issues SET status = 'resolved' WHERE commitment_id = ?", (commitment_id,))
        store.add_event(conn, "commitment", commitment_id, {"status": row["status"]},
                        {"status": body.status}, "후속 행동 결과")
        return {"commitment_id": commitment_id, "status": body.status}


# ───────────────────────── 로컬 데이터 삭제 (R1.9) ─────────────────────────

class DeleteBody(BaseModel):
    confirm: str


@app.delete("/local-data")
def delete_local_data(body: DeleteBody):
    if body.confirm != "삭제":
        raise bad_request("삭제하려면 confirm에 '삭제'를 입력해야 합니다.")
    importer.clear_previews()
    store.delete_all_user_data()
    return {"status": "deleted"}
