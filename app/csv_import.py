"""
CSV 입력 (R3.2, R3.3, R3.8)

표준 거래 CSV를 UTF-8(BOM 허용)로 읽어 행별로 검증한다.
필수 열: date, amount, merchant_name, payment_type, direction
선택 열: account_ref, source_id, category

기획 명세의 열 이름(amount_krw, merchant, payment_method, occurred_at)도 받아준다.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from app import config
from app.errors import bad_request

REQUIRED = ["date", "amount", "merchant_name", "payment_type", "direction"]
_HEADER_ALIASES = {
    "amount_krw": "amount",
    "merchant": "merchant_name",
    "payment_method": "payment_type",
    "occurred_at": "date",
}
_PAYMENT_ALIASES = {
    "credit": "credit", "credit_card": "credit", "card": "credit", "신용": "credit", "신용카드": "credit",
    "debit": "debit", "debit_card": "debit", "체크": "debit", "체크카드": "debit",
    "transfer": "transfer", "bank": "transfer", "이체": "transfer", "계좌이체": "transfer",
}
_DIRECTION_ALIASES = {
    "out": "out", "출금": "out", "지출": "out",
    "in": "in", "입금": "in", "수입": "in",
}
_KST = ZoneInfo(config.TIMEZONE)
_MERCHANT_MAX = 100


@dataclass
class NormalizedRow:
    row_no: int
    date: str
    amount_krw: int
    direction: str
    payment_type: str
    merchant_name: str
    account_ref: Optional[str] = None
    source_id: Optional[str] = None
    category: Optional[str] = None
    time: Optional[str] = None  # HH:MM


@dataclass
class CsvResult:
    rows: list[NormalizedRow] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def mask_long_numbers(text: str) -> str:
    """카드번호·계좌번호처럼 숫자가 10자리 이상인 연속열은 마지막 4자리만 남긴다 (R1.5)."""

    def _mask(m: re.Match) -> str:
        chunk = m.group(0)
        digits = sum(ch.isdigit() for ch in chunk)
        if digits < 10:
            return chunk
        keep_from = digits - 4
        out, seen = [], 0
        for ch in chunk:
            if ch.isdigit():
                out.append(ch if seen >= keep_from else "*")
                seen += 1
            else:
                out.append(ch)
        return "".join(out)

    return re.sub(r"\d[\d\- ]{8,}\d", _mask, text or "")


def clean_merchant(raw: str) -> str:
    name = re.sub(r"\s{2,}", " ", (raw or "").strip())
    return mask_long_numbers(name)[:_MERCHANT_MAX]


def parse_date_kst(raw: str) -> Optional[date]:
    raw = (raw or "").strip()
    if not raw:
        return None
    candidate = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
        if dt.tzinfo is not None:
            dt = dt.astimezone(_KST)
        return dt.date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(raw[:10], fmt).date()
        except ValueError:
            continue
    return None


def parse_time_kst(raw: str) -> Optional[str]:
    """ISO 일시에 시각이 있으면 Asia/Seoul 기준 HH:MM."""
    raw = (raw or "").strip()
    if len(raw) <= 10:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(_KST)
    return dt.strftime("%H:%M")


def today_kst() -> date:
    return datetime.now(_KST).date()


def parse_amount(raw: str) -> Optional[int]:
    text = (raw or "").strip().replace(",", "").replace("원", "").replace(" ", "")
    if not re.fullmatch(r"\d+(\.0+)?", text):
        return None  # 음수, 소수, 문자는 거부. 부호 의미는 direction 열로 받는다
    return int(float(text))


def parse_csv(data: bytes) -> CsvResult:
    if len(data) > config.MAX_CSV_BYTES:
        raise bad_request(f"CSV 파일은 {config.MAX_CSV_BYTES // 1024 // 1024}MB 이하만 올릴 수 있습니다.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise bad_request("CSV는 UTF-8 인코딩만 지원합니다. 엑셀에서 'CSV UTF-8'로 다시 저장해 주세요.")

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise bad_request("CSV 파일이 비어 있습니다.")
    except csv.Error:
        raise bad_request("올바른 CSV 형식이 아닙니다.")

    columns = [_HEADER_ALIASES.get(h.strip().lower(), h.strip().lower()) for h in header]
    missing = [c for c in REQUIRED if c not in columns]
    if missing:
        raise bad_request(
            "CSV에 필수 열이 없습니다: " + ", ".join(missing),
            {"required": REQUIRED, "found": columns},
        )
    index = {name: i for i, name in enumerate(columns)}

    result = CsvResult()
    today = today_kst()
    try:
        for row_no, row in enumerate(reader, start=2):  # 1행은 헤더
            if not row or all(not cell.strip() for cell in row):
                continue
            if row_no - 1 > config.MAX_CSV_ROWS:
                raise bad_request(f"CSV는 {config.MAX_CSV_ROWS:,}행 이하만 올릴 수 있습니다.")

            def cell(name: str) -> str:
                i = index.get(name)
                return row[i].strip() if i is not None and i < len(row) else ""

            problems = []
            for name in REQUIRED:
                if not cell(name):
                    problems.append(f"{name} 값이 비어 있음")
            if problems:
                result.errors.append({"row_no": row_no, "reason": ", ".join(problems)})
                continue

            parsed_date = parse_date_kst(cell("date"))
            amount = parse_amount(cell("amount"))
            payment = _PAYMENT_ALIASES.get(cell("payment_type").lower())
            direction = _DIRECTION_ALIASES.get(cell("direction").lower())

            if parsed_date is None:
                problems.append("date 형식 오류")
            elif parsed_date > today:
                problems.append("미래 날짜")
            if amount is None or amount <= 0:
                problems.append("amount는 양의 정수여야 함")
            elif amount > config.MAX_AMOUNT_KRW:
                problems.append("amount가 999,999,999원 초과")
            if payment is None:
                problems.append("payment_type은 credit, debit, transfer 중 하나")
            if direction is None:
                problems.append("direction은 out, in 중 하나")
            if problems:
                result.errors.append({"row_no": row_no, "reason": ", ".join(problems)})
                continue

            result.rows.append(NormalizedRow(
                row_no=row_no,
                date=parsed_date.isoformat(),
                amount_krw=amount,
                direction=direction,
                payment_type=payment,
                merchant_name=clean_merchant(cell("merchant_name")),
                account_ref=cell("account_ref") or None,
                source_id=cell("source_id") or None,
                category=(cell("category").lower() or None),
                time=parse_time_kst(cell("date")),
            ))
    except csv.Error as e:
        raise bad_request(f"올바른 CSV 형식이 아닙니다. ({e})")
    return result
