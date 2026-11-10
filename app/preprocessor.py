"""
전처리 파이프라인 (Preprocessor)

PDF_Parser 또는 CSV_Parser가 반환한 Transaction 목록에 대해
분석 전 필요한 정제 작업을 수행합니다.

수행 작업:
1. 중복 후보 표시  - 같은 날짜+금액+상호명+방향 조합. 정상 반복 결제가 사라지지 않도록
                     삭제하지 않고 duplicate_indices에 표시만 한다 (R3.5-2)
2. 금액 이상값 필터링  - 0원 이하, 현실적으로 불가능한 초고액
3. 상호명 정규화  - 앞뒤 공백 제거, 특수문자 정리, 빈 값 → '알 수 없음'
4. 날짜 범위 검증  - 미래 날짜 제거
5. 정렬  - 날짜 오름차순
"""

from __future__ import annotations

import re
from datetime import date

from app.models import ParseResult, Transaction

# 1회 거래 최대 금액. 금액 입력 상한(R2.2)과 같게 999,999,999원
MAX_AMOUNT = 999_999_999

# 상호명 정제용 패턴: 괄호 안 내용, 연속 공백, 불필요한 특수문자 제거
_CLEAN_PATTERN = re.compile(r"[^\w가-힣\s\-&.]")
_SPACE_PATTERN = re.compile(r"\s{2,}")


def _normalize_merchant(raw: str) -> str:
    """
    상호명을 정규화합니다.
    - 앞뒤 공백 제거
    - 연속 공백 → 단일 공백
    - 알 수 없는 특수문자 제거
    - 빈 문자열 → '알 수 없음'
    """
    cleaned = _CLEAN_PATTERN.sub("", raw).strip()
    cleaned = _SPACE_PATTERN.sub(" ", cleaned)
    return cleaned if cleaned else "알 수 없음"


def _is_duplicate(seen: set[tuple], txn: Transaction) -> bool:
    """
    같은 날짜 + 금액 + 상호명 + 방향 조합의 중복 여부를 확인합니다.
    """
    key = (txn.date, txn.time, txn.amount, txn.merchant_name, txn.direction)
    if key in seen:
        return True
    seen.add(key)
    return False


def preprocess(result: ParseResult) -> ParseResult:
    """
    ParseResult의 Transaction 목록을 전처리합니다.

    Args:
        result: pdf_parser 또는 csv_parser가 반환한 ParseResult

    Returns:
        전처리가 완료된 새로운 ParseResult
        (원본 result는 변경하지 않음)
    """
    today = date.today()
    cleaned: list[Transaction] = []
    additional_skipped = 0
    seen: set[tuple] = set()
    warnings: list[str] = list(result.warnings)

    for txn in result.transactions:
        # 1. 금액 이상값 필터링
        if txn.amount <= 0:
            additional_skipped += 1
            warnings.append(
                f"[금액 오류] {txn.date} {txn.merchant_name}: "
                f"금액이 0 이하입니다 ({txn.amount}원). 스킵합니다."
            )
            continue

        if txn.amount > MAX_AMOUNT:
            additional_skipped += 1
            warnings.append(
                f"[금액 이상값] {txn.date} {txn.merchant_name}: "
                f"금액이 999,999,999원을 초과합니다 ({txn.amount:,.0f}원). 스킵합니다."
            )
            continue

        # 2. 미래 날짜 제거
        if txn.date > today:
            additional_skipped += 1
            warnings.append(
                f"[날짜 오류] {txn.merchant_name}: "
                f"미래 날짜({txn.date})의 거래입니다. 스킵합니다."
            )
            continue

        # 3. 상호명 정규화 (새 Transaction 객체 생성)
        normalized_merchant = _normalize_merchant(txn.merchant_name)
        normalized_txn = Transaction(
            date=txn.date,
            amount=int(txn.amount),
            merchant_name=normalized_merchant,
            payment_type=txn.payment_type,
            direction=txn.direction,
            time=txn.time,
        )

        cleaned.append(normalized_txn)

    # 4. 날짜 오름차순 정렬 (안정 정렬이라 같은 날짜는 원래 순서 유지)
    cleaned.sort(key=lambda t: (t.date, t.time or ""))

    # 5. 중복 후보 표시. 삭제하지 않는다 (R3.5-2)
    duplicate_indices: list[int] = []
    for i, txn in enumerate(cleaned):
        if _is_duplicate(seen, txn):
            duplicate_indices.append(i)
            warnings.append(
                f"[중복 후보] {txn.date} {txn.merchant_name} "
                f"{txn.amount:,.0f}원: 같은 거래가 이미 있습니다. 가져오기 전에 확인하세요."
            )

    return ParseResult(
        transactions=cleaned,
        skipped_rows=result.skipped_rows + additional_skipped,
        warnings=warnings,
        duplicate_indices=duplicate_indices,
    )
