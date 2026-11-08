"""
도메인 모델 정의
- Transaction: 단일 거래 항목
- ParseResult: 파싱/변환 결과 (성공한 행 + 스킵된 행 수)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


class PaymentType(str, Enum):
    CREDIT = "credit"      # 신용카드
    DEBIT = "debit"        # 체크카드
    TRANSFER = "transfer"  # 계좌이체


# 내부 CSV 컬럼 순서 (Requirement 7 AC #3)
CSV_COLUMNS = ["date", "amount", "merchant_name", "payment_type"]


@dataclass
class Transaction:
    date: date
    amount: float          # 원화 금액 (양수)
    merchant_name: str
    payment_type: PaymentType

    def to_csv_row(self) -> dict:
        """내부 CSV 형식 딕셔너리로 변환"""
        return {
            "date": self.date.isoformat(),
            "amount": self.amount,
            "merchant_name": self.merchant_name,
            "payment_type": self.payment_type.value,
        }


@dataclass
class ParseResult:
    transactions: list[Transaction] = field(default_factory=list)
    skipped_rows: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return len(self.transactions) + self.skipped_rows
