"""
도메인 모델 정의
- Transaction: 단일 거래 항목
- ParseResult: 파싱/변환 결과 (성공한 행 + 스킵된 행 수 + 중복 후보)
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


class Direction(str, Enum):
    """입출금 방향 (R3.5-1). 금액은 항상 양수이고 부호 의미는 direction으로 표현한다."""
    OUT = "out"  # 돈이 나감 (지출, 출금)
    IN = "in"    # 돈이 들어옴 (입금, 환불)


# 표준 거래 CSV 컬럼 순서 (requirements R3.2)
CSV_COLUMNS = ["date", "amount", "merchant_name", "payment_type", "direction"]


@dataclass
class Transaction:
    date: date
    amount: int            # 원화 정수 금액 (양수) (R3.5-3)
    merchant_name: str
    payment_type: PaymentType
    direction: Direction = Direction.OUT
    time: Optional[str] = None  # HH:MM (있으면)

    def to_csv_row(self) -> dict:
        """표준 CSV 형식 딕셔너리로 변환"""
        return {
            "date": self.date.isoformat(),
            "amount": int(self.amount),
            "merchant_name": self.merchant_name,
            "payment_type": self.payment_type.value,
            "direction": self.direction.value,
        }


@dataclass
class ParseResult:
    transactions: list[Transaction] = field(default_factory=list)
    skipped_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    # 같은 날짜·금액·상호명·방향인 거래의 인덱스 (자동 삭제하지 않고 후보로만 표시, R3.5-2)
    duplicate_indices: list[int] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return len(self.transactions) + self.skipped_rows
