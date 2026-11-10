"""
CSV 변환기

전처리가 완료된 Transaction 목록을 내부 CSV 형식(문자열)으로 직렬화합니다.
컬럼 순서 = date, amount, merchant_name, payment_type, direction (requirements R3.2)
"""

from __future__ import annotations

import csv
import io

from app.models import CSV_COLUMNS, ParseResult, Transaction


def transactions_to_csv(transactions: list[Transaction]) -> str:
    """
    Transaction 목록을 CSV 문자열로 변환합니다.

    Returns:
        UTF-8 CSV 문자열 (헤더 포함)
    """
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for txn in transactions:
        writer.writerow(txn.to_csv_row())
    return output.getvalue()


def transactions_to_csv_bytes(transactions: list[Transaction]) -> bytes:
    """
    Transaction 목록을 UTF-8 인코딩된 CSV 바이트로 변환합니다.
    (파일 다운로드 또는 추가 파이프라인 전달용)
    """
    return transactions_to_csv(transactions).encode("utf-8-sig")  # BOM 포함 (엑셀 호환)
