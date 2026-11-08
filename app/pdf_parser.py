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

from app.models import CSV_COLUMNS, ParseResult, PaymentType, Transaction

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


def _parse_amount(raw: str) -> Optional[float]:
    """금액 문자열(쉼표 포함)을 float으로 변환합니다."""
    raw = raw.strip().replace(",", "").replace("원", "").replace(" ", "")
    # 음수(-)는 절대값으로 처리 (지출은 항상 양수로 저장)
    raw = raw.lstrip("-")
    try:
        value = float(raw)
        return value if value > 0 else None
    except ValueError:
        return None


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
        return [], len(rows)

    for row in rows:
        if not row or all(cell is None or str(cell).strip() == "" for cell in row):
            continue  # 빈 행 스킵

        try:
            # 각 필드 추출
            raw_date = _get_cell(row, col_map.get("date"))
            raw_amount = _get_cell(row, col_map.get("amount"))
            raw_merchant = _get_cell(row, col_map.get("merchant_name"))
            raw_payment = _get_cell(row, col_map.get("payment_type"))

            # 필수 필드 누락 체크 (Requirement 7 AC #7)
            if not all([raw_date, raw_amount, raw_merchant, raw_payment]):
                skipped += 1
                continue

            parsed_date = _parse_date(raw_date)
            parsed_amount = _parse_amount(raw_amount)
            parsed_payment = _normalize_payment_type(raw_payment)

            if parsed_date is None or parsed_amount is None or parsed_payment is None:
                skipped += 1
                continue

            transactions.append(
                Transaction(
                    date=parsed_date,
                    amount=parsed_amount,
                    merchant_name=raw_merchant.strip(),
                    payment_type=parsed_payment,
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
    header_lower = [str(h).lower().replace(" ", "") if h else "" for h in header]

    # date 컬럼
    date_keys = ["date", "날짜", "거래일", "거래일자", "이용일", "결제일"]
    for key in date_keys:
        for i, h in enumerate(header_lower):
            if key in h:
                mapping["date"] = i
                break
        if "date" in mapping:
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
    merchant_keys = ["merchant", "상호", "가맹점", "거래처", "내용", "적요", "이용가맹점"]
    for key in merchant_keys:
        for i, h in enumerate(header_lower):
            if key in h:
                mapping["merchant_name"] = i
                break
        if "merchant_name" in mapping:
            break

    # payment_type 컬럼
    payment_keys = ["payment", "구분", "거래구분", "유형", "종류", "type"]
    for key in payment_keys:
        for i, h in enumerate(header_lower):
            if key in h:
                mapping["payment_type"] = i
                break
        if "payment_type" in mapping:
            break

    # 4개 컬럼이 모두 매핑됐는지 확인
    required = {"date", "amount", "merchant_name", "payment_type"}
    if not required.issubset(mapping.keys()):
        return None

    return mapping


def parse_pdf(file_bytes: bytes) -> ParseResult:
    """
    PDF 바이트 데이터를 파싱하여 거래 내역을 추출합니다.

    Args:
        file_bytes: 업로드된 PDF 파일의 바이트 데이터

    Returns:
        ParseResult: 추출된 Transaction 목록과 스킵된 행 수

    Raises:
        PDFParseError: 파일 크기 초과, 스캔 PDF, 파싱 실패 등
    """
    # 파일 크기 검증 (Requirement 7 AC #5)
    if len(file_bytes) > MAX_PDF_SIZE_BYTES:
        raise PDFParseError(
            f"PDF 파일 크기가 20MB를 초과합니다. "
            f"현재 크기: {len(file_bytes) / 1024 / 1024:.1f}MB"
        )

    try:
        pdf = pdfplumber.open(io.BytesIO(file_bytes))
    except Exception as e:
        raise PDFParseError(f"PDF 파일을 열 수 없습니다. 올바른 PDF 형식인지 확인해 주세요. ({e})")

    with pdf:
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

        match = date_start_pattern.search(line)
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
            )
        )

    return transactions, skipped
