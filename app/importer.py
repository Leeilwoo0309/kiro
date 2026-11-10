"""
Import_Service (R3)

1. preview: 파일을 표준 거래 형식으로 바꾸고 유효 행·건너뛴 행·중복 후보를 돌려준다.
   정규화된 행은 메모리에만 잠깐 두고 원본 파일은 어디에도 저장하지 않는다 (R1.4).
2. commit: 사용자가 확인한 행만 저장하고 분류(R4)와 거래 성격 후보(R5.2)를 붙인다.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import asdict
from typing import Optional

from app import classifier, config, ledger, store
from app.csv_import import NormalizedRow, clean_merchant, parse_csv
from app.errors import bad_request, not_found, unprocessable
from app.pdf_parser import PDFParseError
from app.pipeline import run_pdf_pipeline

_PREVIEW_TTL_SEC = 30 * 60
_previews: dict[str, dict] = {}
_lock = threading.Lock()


def _purge_expired() -> None:
    now = time.time()
    for key in [k for k, v in _previews.items() if now - v["created"] > _PREVIEW_TTL_SEC]:
        _previews.pop(key, None)


def clear_previews() -> None:
    with _lock:
        _previews.clear()


def _rows_from_pdf(data: bytes, password: Optional[str]) -> tuple[list[NormalizedRow], list[dict], set[int]]:
    if len(data) > config.MAX_PDF_BYTES:
        raise bad_request("PDF 파일은 20MB 이하만 올릴 수 있습니다.")
    try:
        result = run_pdf_pipeline(data, password=password)
    except PDFParseError as e:
        raise unprocessable(str(e))
    rows = [
        NormalizedRow(
            row_no=i + 1,
            date=t.date.isoformat(),
            amount_krw=int(t.amount),
            direction=t.direction.value,
            payment_type=t.payment_type.value,
            merchant_name=clean_merchant(t.merchant_name),
            time=t.time,
        )
        for i, t in enumerate(result.transactions)
    ]
    errors = [{"row_no": None, "reason": w} for w in result.warnings if not w.startswith("[중복 후보]")]
    if result.skipped_rows and not errors:
        errors.append({"row_no": None, "reason": f"필수 값이 없거나 읽을 수 없는 행 {result.skipped_rows}개"})
    return rows, errors, set(result.duplicate_indices)


def _account_holder(data: bytes, password: Optional[str]) -> Optional[str]:
    """은행 명세서 머리말의 '예금주'를 찾는다. 본인 이체 판별(R5.2)에만 쓰고 로컬 설정에만 저장한다."""
    try:
        import io
        import pdfplumber

        with pdfplumber.open(io.BytesIO(data), password=password or "") as pdf:
            text = pdf.pages[0].extract_text() or "" if pdf.pages else ""
    except Exception:
        return None
    m = re.search(r"예금주\s*[:：]?\s*([가-힣A-Za-z]{2,20})", text)
    return m.group(1) if m else None


def preview(filename: str, data: bytes, password: Optional[str] = None) -> dict:
    """password는 암호 PDF를 여는 데만 쓰고 저장·기록하지 않는다."""
    name = (filename or "").lower()
    holder: Optional[str] = None
    if name.endswith(".csv"):
        kind = "csv"
        parsed = parse_csv(data)
        rows, errors = parsed.rows, parsed.errors
        file_dups: set[int] = set()
        seen = set()
        for i, r in enumerate(rows):
            key = (r.date, r.time, r.amount_krw, r.merchant_name, r.direction)
            if key in seen:
                file_dups.add(i)
            seen.add(key)
        skipped = len(errors)
    elif name.endswith(".pdf"):
        kind = "pdf"
        rows, errors, file_dups = _rows_from_pdf(data, password)
        holder = _account_holder(data, password)
        skipped = None
    else:
        raise bad_request("CSV(.csv) 또는 PDF(.pdf) 파일만 올릴 수 있습니다.")

    if not rows:
        raise unprocessable(
            "거래를 찾지 못했습니다. 파일 형식(열 이름, 인코딩)이 맞는지 확인해 주세요.",
            {"errors": errors[:50]},
        )

    with store.read() as conn:
        existing = {
            (r["date"], r["amount_krw"], r["merchant_name"], r["direction"])
            for r in conn.execute("SELECT date, amount_krw, merchant_name, direction FROM transactions")
        }
        existing_sources = {
            (r["account_ref"], r["source_id"])
            for r in conn.execute("SELECT account_ref, source_id FROM transactions WHERE source_id IS NOT NULL")
        }

    preview_rows = []
    for i, r in enumerate(rows):
        dup_reason: Optional[str] = None
        if r.source_id and (r.account_ref, r.source_id) in existing_sources:
            dup_reason = "같은 원천 ID가 이미 저장됨"
        elif (r.date, r.amount_krw, r.merchant_name, r.direction) in existing:
            dup_reason = "이미 저장된 거래와 날짜·금액·거래처가 같음"
        elif i in file_dups:
            dup_reason = "파일 안에 날짜·금액·거래처가 같은 거래가 있음 (정상 반복 결제일 수 있음)"
        item = asdict(r)
        item["idx"] = i
        item["duplicate_reason"] = dup_reason
        # 원천 ID가 같은 거래는 기본 제외, 나머지 중복 후보는 사용자가 고른다 (R3.5-2)
        item["default_excluded"] = dup_reason == "같은 원천 ID가 이미 저장됨"
        preview_rows.append(item)

    preview_id = store.new_id("preview")
    with _lock:
        _purge_expired()
        _previews[preview_id] = {
            "kind": kind,
            "file_hash": hashlib.sha256(data).hexdigest(),
            "rows": rows,
            "errors": errors,
            "holder": holder,
            "created": time.time(),
        }

    return {
        "preview_id": preview_id,
        "kind": kind,
        "valid_count": len(rows),
        "skipped_count": skipped if skipped is not None else len(errors),
        "errors": errors[:200],
        "duplicate_count": sum(1 for x in preview_rows if x["duplicate_reason"]),
        "rows": preview_rows,
        "ai_notice": "카테고리 분류와 감사 질의를 위해 거래처명 등 일부 정보가 BAZE(Mindlogic) 서버로 전송됩니다.",
        # 이름 자체는 돌려주지 않는다. 가져오기 확정 시 본인 이체 판별용으로 로컬 설정에만 저장
        "account_holder_detected": bool(holder),
    }


def commit(preview_id: str, exclude_idx: list[int], use_ai: bool) -> dict:
    with _lock:
        pv = _previews.pop(preview_id, None)
    if pv is None:
        raise not_found("미리보기가 없거나 만료되었습니다. 파일을 다시 올려 주세요.")

    excluded = set(exclude_idx or [])
    rows: list[NormalizedRow] = [r for i, r in enumerate(pv["rows"]) if i not in excluded]
    if not rows:
        raise bad_request("가져올 거래가 없습니다.")

    # 1) 규칙 분류. 남은 것만 모델로 (R4.3)
    tx_ids = [store.new_id("tx") for _ in rows]
    categories: dict[str, tuple[Optional[str], str, str]] = {}
    unmatched = []
    for tx_id, r in zip(tx_ids, rows):
        if r.direction != "out":
            categories[tx_id] = (None, "rule", "confirmed")
            continue
        if r.category in classifier.CATEGORIES:  # R4.8
            categories[tx_id] = (r.category, "rule", "pending")
            continue
        cat = classifier.classify_by_rule(r.merchant_name)
        if cat:
            categories[tx_id] = (cat, "rule", "pending")
        else:
            unmatched.append({"id": tx_id, "merchant_name": r.merchant_name})
    model_result = classifier.classify_with_model(unmatched) if use_ai else {}
    for item in unmatched:
        cat = model_result.get(item["id"])
        categories[item["id"]] = (cat, "model", "pending") if cat else ("other", "rule", "pending")

    with store.tx() as conn:
        owner_names = store.get_setting(conn, "owner_names") or []
        if pv.get("holder") and pv["holder"] not in owner_names:
            owner_names = owner_names + [pv["holder"]]
            store.set_setting(conn, "owner_names", owner_names)
        import_id = store.new_id("import")
        counts = {"inserted": len(rows), "excluded": len(excluded), "errors": len(pv["errors"]),
                  "rule": 0, "model": 0, "other_pending": 0}
        conn.execute(
            "INSERT INTO imports(id, kind, file_hash, row_counts, created_at) VALUES (?, ?, ?, ?, ?)",
            (import_id, pv["kind"], pv["file_hash"], store.dumps(counts), store.now_iso()),
        )
        for tx_id, r in zip(tx_ids, rows):
            conn.execute(
                "INSERT INTO transactions(id, import_id, date, time, amount_krw, direction, payment_type, "
                "merchant_name, account_ref, source_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tx_id, import_id, r.date, r.time, r.amount_krw, r.direction, r.payment_type,
                 r.merchant_name, r.account_ref, r.source_id),
            )
            econ, econ_status, flags = ledger.initial_economic_type(
                r.direction, r.payment_type, r.merchant_name, owner_names)
            cat, source, review = categories[tx_id]
            if r.direction == "out" and econ != "expense":
                cat = cat or "other"
            conn.execute(
                "INSERT INTO interpretations(transaction_id, category, economic_type, classification_source, "
                "review_status, economic_status, flags) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tx_id, cat, econ, source, review, econ_status, store.dumps(flags)),
            )
            if r.direction == "out":
                if source == "model":
                    counts["model"] += 1
                elif cat == "other":
                    counts["other_pending"] += 1
                else:
                    counts["rule"] += 1
        conn.execute("UPDATE imports SET row_counts = ? WHERE id = ?", (store.dumps(counts), import_id))
        store.add_event(conn, "import", import_id, None, {"rows": len(rows)}, "파일 가져오기")
        version = store.bump_ledger_version(conn)

    return {"import_id": import_id, "counts": counts, "ledger_version": version,
            "model_used": bool(model_result)}
