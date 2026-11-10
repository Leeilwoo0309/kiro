"""
PDF_Parser: 텍스트 기반 PDF에서 거래 내역을 추출하는 컴포넌트

지원 형식:
- 텍스트 레이어가 있는 PDF (은행/카드사 명세서 등)
- 스캔 이미지 PDF는 거부 (Requirement 7 AC #4)

파일 크기 제한: 20MB (Requirement 7 AC #5)
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime
from typing import Optional

import pdfplumber

from app.models import CSV_COLUMNS, Direction, ParseResult, PaymentType, Transaction

# 20 MB
MAX_PDF_SIZE_BYTES = 20 * 1024 * 1024

# 거래 유형 정규화 매핑 (한글 → PaymentType)
# 주의: 더 구체적인 키(체크카드, 신용카드)를 앞에 두어 부분 매칭 충돌 방지
_PAYMENT_TYPE_MAP: dict[str, PaymentType] = {
    # 체크카드 계열 (신용보다 먼저 — '체크카드' 안에 '카드' 포함되므로)
    "체크카드": PaymentType.DEBIT,
    "체크": PaymentType.DEBIT,
    "debit": PaymentType.DEBIT,
    # 신용카드 계열
    "신용카드": PaymentType.CREDIT,
    "신용": PaymentType.CREDIT,
    "credit": PaymentType.CREDIT,
    "카드": PaymentType.CREDIT,   # 앞의 더 구체적 키에 미매칭된 경우만 도달
    # 계좌이체 계열
    "이체": PaymentType.TRANSFER,
    "transfer": PaymentType.TRANSFER,
    "출금": PaymentType.TRANSFER,
    "입금": PaymentType.TRANSFER,
}

# 날짜 패턴 목록 (우선순위 순)
_DATE_PATTERNS = [
    (r"\d{4}[-./]\d{2}[-./]\d{2}", "%Y-%m-%d"),   # 2024-01-15
    (r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", "%Y-%m-%d"),
    (r"\d{2}[-./]\d{2}[-./]\d{2}", "%y-%m-%d"),    # 24-01-15
    (r"\d{4}\d{2}\d{2}", "%Y%m%d"),                 # 20240115
]

# 금액 패턴: 1,234,567 또는 1234567
_AMOUNT_PATTERN = re.compile(r"[\d,]+")


class PDFParseError(Exception):
    """PDF 파싱 실패 시 발생하는 예외"""


def _detect_scanned_pdf(pdf: pdfplumber.PDF) -> bool:
    """
    스캔 이미지 PDF 여부를 감지합니다.
    텍스트가 전혀 없고 이미지만 있는 경우 스캔 PDF로 판단합니다.
    """
    for page in pdf.pages:
        if page.extract_text():
            return False  # 텍스트가 한 페이지라도 있으면 텍스트 기반 PDF
    return True


def _parse_date(raw: str) -> Optional[date]:
    """다양한 날짜 형식 문자열을 date 객체로 변환합니다."""
    raw = raw.strip().replace("/", "-").replace(".", "-")
    for pattern, fmt in _DATE_PATTERNS:
        match = re.search(pattern.replace("[-./]", "[-]"), raw)
        if match:
            try:
                return datetime.strptime(match.group(), fmt).date()
            except ValueError:
                continue
    return None


def _parse_time(raw: str) -> Optional[str]:
    """'2026.10.23 12:18:47' 같은 거래일시에서 HH:MM을 뽑는다. 새벽 결제 찾기에 쓴다."""
    m = re.search(r"\b(\d{1,2}):(\d{2})(?::\d{2})?\b", raw or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _parse_amount(raw: str) -> Optional[int]:
    """금액 문자열(쉼표 포함)을 원화 정수로 변환합니다 (R3.5-3).

    부호는 버리고 절대값을 반환합니다. 부호의 의미는 _detect_direction이 판단합니다.
    """
    raw = raw.strip().replace(",", "").replace("원", "").replace(" ", "")
    raw = raw.lstrip("-")
    try:
        value = int(round(float(raw)))
        return value if value > 0 else None
    except ValueError:
        return None


# 입금(돈이 들어옴)으로 판단하는 키워드. 카드 명세서의 취소·환불도 돈이 돌아오는 방향이다.
_IN_KEYWORDS = ("입금", "환불", "취소", "맡기신")


def _detect_direction(payment_text: str, amount_text: str) -> Direction:
    """입출금 방향을 판단합니다 (R3.5-1).

    1. 거래구분 텍스트에 입금·환불·취소가 있으면 IN
    2. 금액이 음수로 표기되어 있으면 IN (카드 명세서의 취소·환불 표기 관례)
    3. 그 외는 OUT
    """
    text = (payment_text or "").lower()
    if any(k in text for k in _IN_KEYWORDS):
        return Direction.IN
    if (amount_text or "").strip().startswith("-"):
        return Direction.IN
    return Direction.OUT


def _normalize_payment_type(raw: str) -> Optional[PaymentType]:
    """거래 유형 문자열을 PaymentType Enum으로 정규화합니다."""
    raw_lower = raw.strip().lower()
    for key, ptype in _PAYMENT_TYPE_MAP.items():
        if key in raw_lower:
            return ptype
    return None


def _extract_transactions_from_table(
    rows: list[list[Optional[str]]],
    header: list[str],
) -> tuple[list[Transaction], int]:
    """
    테이블 행 목록에서 Transaction 리스트를 추출합니다.

    Returns:
        (transactions, skipped_count)
    """
    transactions: list[Transaction] = []
    skipped = 0

    # 헤더에서 컬럼 인덱스 탐색 (유연한 매핑)
    col_map = _map_columns(header)
    if col_map is None:
        # 계좌 정보 표처럼 거래 표가 아닌 표는 건너뛴 행으로 세지 않는다
        return [], 0

    for row in rows:
        if not row or all(cell is None or str(cell).strip() == "" for cell in row):
            continue  # 빈 행 스킵

        try:
            # 각 필드 추출
            raw_date = _get_cell(row, col_map.get("date"))
            raw_amount = _get_cell(row, col_map.get("amount"))
            raw_merchant = _get_cell(row, col_map.get("merchant_name"))
            raw_payment = _get_cell(row, col_map.get("payment_type"))
            raw_in = _get_cell(row, col_map.get("in_amount"))
            raw_out = _get_cell(row, col_map.get("out_amount"))
            has_in_out = "in_amount" in col_map

            # 입금액·출금액 열이 따로 있는 은행 명세서: 값이 있는 열이 방향을 정한다 (R3.5-1)
            in_amt = _parse_amount(raw_in) if raw_in else None
            out_amt = _parse_amount(raw_out) if raw_out else None
            if in_amt:
                parsed_amount, direction = in_amt, Direction.IN
            elif out_amt:
                parsed_amount, direction = out_amt, Direction.OUT
            else:
                parsed_amount = _parse_amount(raw_amount) if raw_amount else None
                direction = _detect_direction(raw_payment or "", raw_amount or "")

            parsed_payment = _normalize_payment_type(raw_payment) if raw_payment else None
            if parsed_payment is None and has_in_out:
                # 은행 계좌 명세서에서 체크카드가 아닌 거래(전자금융, 자동이체 등)는 계좌 거래로 본다
                parsed_payment = PaymentType.TRANSFER
            if raw_merchant:
                raw_merchant = " ".join(raw_merchant.split())  # 셀 안 줄바꿈 제거

            # 필수 필드 누락 체크 (Requirement 7 AC #7)
            if not raw_date or not raw_merchant:
                skipped += 1
                continue

            parsed_date = _parse_date(raw_date)

            if parsed_date is None or parsed_amount is None or parsed_payment is None:
                skipped += 1
                continue

            transactions.append(
                Transaction(
                    date=parsed_date,
                    amount=parsed_amount,
                    merchant_name=raw_merchant.strip(),
                    payment_type=parsed_payment,
                    direction=direction,
                    time=_parse_time(_get_cell(row, col_map.get("time")) or raw_date),
                )
            )
        except Exception:
            skipped += 1
            continue

    return transactions, skipped


def _get_cell(row: list, index: Optional[int]) -> Optional[str]:
    """행에서 안전하게 셀 값을 꺼냅니다."""
    if index is None or index >= len(row):
        return None
    val = row[index]
    return str(val).strip() if val is not None else None


def _map_columns(header: list[str]) -> Optional[dict[str, int]]:
    """
    헤더 행에서 필수 컬럼의 인덱스를 탐색합니다.
    한글/영어 헤더 모두 지원합니다.
    """
    mapping = {}
    header_lower = [re.sub(r"\s+", "", str(h).lower()) if h else "" for h in header]

    # date 컬럼
    date_keys = ["date", "날짜", "거래일", "거래일자", "이용일", "결제일"]
    for key in date_keys:
        for i, h in enumerate(header_lower):
            if key in h:
                mapping["date"] = i
                break
        if "date" in mapping:
            break

    for i, h in enumerate(header_lower):
        if "시간" in h or h == "time":
            mapping["time"] = i
            break

    # amount 컬럼
    amount_keys = ["amount", "금액", "이용금액", "거래금액", "출금액", "결제금액"]
    for key in amount_keys:
        for i, h in enumerate(header_lower):
            if key in h:
                mapping["amount"] = i
                break
        if "amount" in mapping:
            break

    # merchant_name 컬럼
    # 은행 명세서(예: KB국민은행)는 '적요'에 거래 수단(체크카드, 전자금융)이 들어가고
    # 실제 거래처는 '보낸분/받는분'에 있으므로 그 열을 먼저 찾는다.
    merchant_keys = ["merchant", "보낸분/받는분", "받는분", "보낸분", "상호", "가맹점", "거래처",
                     "내용", "적요", "이용가맹점"]
    for key in merchant_keys:
        for i, h in enumerate(header_lower):
            if key in h:
                mapping["merchant_name"] = i
                break
        if "merchant_name" in mapping:
            break

    # payment_type 컬럼. 거래처 열로 쓰지 않은 '적요'는 거래 수단 열로 쓴다.
    payment_keys = ["payment", "구분", "거래구분", "유형", "종류", "type", "적요"]
    for key in payment_keys:
        for i, h in enumerate(header_lower):
            if key in h and i != mapping.get("merchant_name"):
                mapping["payment_type"] = i
                break
        if "payment_type" in mapping:
            break

    # 입금액·출금액 분리 열 (은행 명세서). 선택 항목이며 방향 판단에 사용 (R3.5-1)
    in_keys = ["입금액", "입금", "맡기신"]
    out_keys = ["출금액", "출금", "찾으신"]
    for target, keys in (("in_amount", in_keys), ("out_amount", out_keys)):
        for key in keys:
            for i, h in enumerate(header_lower):
                if key in h:
                    mapping[target] = i
                    break
            if target in mapping:
                break
    # 입금·출금 열이 둘 다 있어야 분리 형식으로 인정한다
    if not {"in_amount", "out_amount"}.issubset(mapping.keys()):
        mapping.pop("in_amount", None)
        mapping.pop("out_amount", None)

    # 필수 컬럼 확인. 입금·출금 분리 열이 있으면 거래구분 열은 없어도 된다.
    required = {"date", "merchant_name"}
    if "in_amount" not in mapping:
        required.update(("amount", "payment_type"))
    if not required.issubset(mapping.keys()):
        return None

    return mapping


def _is_password_error(e: Exception) -> bool:
    return "password" in type(e).__name__.lower()


def parse_pdf(file_bytes: bytes, password: Optional[str] = None) -> ParseResult:
    """
    PDF 바이트 데이터를 파싱하여 거래 내역을 추출합니다.

    Args:
        file_bytes: 업로드된 PDF 파일의 바이트 데이터
        password: 암호가 걸린 PDF의 비밀번호 (은행 거래내역 PDF 등). 저장·기록하지 않는다.

    Returns:
        ParseResult: 추출된 Transaction 목록과 스킵된 행 수

    Raises:
        PDFParseError: 파일 크기 초과, 스캔 PDF, 비밀번호 오류, 파싱 실패 등
    """
    # 파일 크기 검증 (Requirement 7 AC #5)
    if len(file_bytes) > MAX_PDF_SIZE_BYTES:
        raise PDFParseError(
            f"PDF 파일 크기가 20MB를 초과합니다. "
            f"현재 크기: {len(file_bytes) / 1024 / 1024:.1f}MB"
        )

    try:
        pdf = pdfplumber.open(io.BytesIO(file_bytes), password=password or "")
    except Exception as e:
        if _is_password_error(e):
            raise PDFParseError("PDF 비밀번호가 필요하거나 틀렸습니다. 비밀번호를 확인해 주세요.")
        raise PDFParseError(f"PDF 파일을 열 수 없습니다. 올바른 PDF 형식인지 확인해 주세요. ({type(e).__name__})")

    try:
        with pdf:
            return _parse_opened_pdf(pdf)
    except PDFParseError:
        raise
    except Exception as e:
        if _is_password_error(e):
            raise PDFParseError("PDF 비밀번호가 필요하거나 틀렸습니다. 비밀번호를 확인해 주세요.")
        raise PDFParseError(f"PDF를 읽는 중 오류가 났습니다. ({type(e).__name__})")


def _parse_opened_pdf(pdf) -> ParseResult:
        """열린 PDF에서 표 → 텍스트 순서로 거래를 추출한다."""
        # 스캔 이미지 PDF 감지 (Requirement 7 AC #4)
        if _detect_scanned_pdf(pdf):
            raise PDFParseError(
                "스캔된 이미지 기반 PDF는 지원하지 않습니다. "
                "텍스트 레이어가 포함된 PDF를 업로드해 주세요."
            )

        all_transactions: list[Transaction] = []
        total_skipped = 0

        for page in pdf.pages:
            # 테이블 추출 시도
            tables = page.extract_tables()
            for table in tables:
                if not table or len(table) < 2:
                    continue
                header = table[0]
                rows = table[1:]
                txns, skipped = _extract_transactions_from_table(rows, header)
                all_transactions.extend(txns)
                total_skipped += skipped

        # 테이블에서 추출 실패한 경우 텍스트 파싱 시도
        if not all_transactions:
            for page in pdf.pages:
                text = page.extract_text() or ""
                txns, skipped = _parse_text_fallback(text)
                all_transactions.extend(txns)
                total_skipped += skipped

        # 아무것도 추출하지 못한 경우 (Requirement 7 AC #6)
        if not all_transactions and total_skipped == 0:
            raise PDFParseError(
                "PDF에서 거래 내역을 찾을 수 없습니다. "
                "파일 형식이 올바른지 확인해 주세요."
            )

        return ParseResult(
            transactions=all_transactions,
            skipped_rows=total_skipped,
        )


def _parse_text_fallback(text: str) -> tuple[list[Transaction], int]:
    """
    테이블 추출에 실패했을 때 원시 텍스트에서 패턴 매칭으로 거래 내역을 추출합니다.
    날짜 + 금액 + 상호명이 같은 줄에 있는 형태를 탐지합니다.
    """
    transactions: list[Transaction] = []
    skipped = 0

    # 날짜로 시작하는 라인만 후보로 처리
    date_start_pattern = re.compile(
        r"(\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{2}[-./]\d{2}[-./]\d{2})"
        r"(.+?)"
        r"([\d,]{3,})"              # 금액
        r"\s*(신용|체크|이체|출금|카드|transfer|credit|debit)?",
        re.IGNORECASE,
    )

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        match = date_start_pattern.match(line)
        if not match:
            continue

        raw_date_str = match.group(1)
        raw_middle = match.group(2)
        raw_amount_str = match.group(3)
        raw_payment_str = match.group(4) or ""

        parsed_date = _parse_date(raw_date_str)
        parsed_amount = _parse_amount(raw_amount_str)
        parsed_payment = _normalize_payment_type(raw_payment_str) if raw_payment_str else None

        # 결제 유형 추론이 안 되면 텍스트에서 재탐색
        if parsed_payment is None:
            for key, ptype in _PAYMENT_TYPE_MAP.items():
                if key in line.lower():
                    parsed_payment = ptype
                    break
        if parsed_payment is None:
            parsed_payment = PaymentType.CREDIT  # 기본값

        merchant = raw_middle.strip().split()[0] if raw_middle.strip() else ""
        if not merchant:
            skipped += 1
            continue

        if parsed_date is None or parsed_amount is None:
            skipped += 1
            continue

        transactions.append(
            Transaction(
                date=parsed_date,
                amount=parsed_amount,
                merchant_name=merchant,
                payment_type=parsed_payment,
                direction=_detect_direction(line, ""),
            )
        )

    return transactions, skipped
