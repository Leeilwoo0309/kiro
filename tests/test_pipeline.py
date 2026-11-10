"""
PDF → CSV 변환 + 전처리 파이프라인 단위 테스트

외부 PDF 파일 없이 로직을 검증하기 위해
pdfplumber를 mock 처리합니다.
"""

from __future__ import annotations

import io
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.models import ParseResult, PaymentType, Transaction
from app.pdf_parser import (
    PDFParseError,
    _normalize_payment_type,
    _parse_amount,
    _parse_date,
    _map_columns,
)
from app.preprocessor import preprocess


# ─────────────────────────────────────────────
# _parse_date 테스트
# ─────────────────────────────────────────────

class TestParseDate:
    def test_iso_format(self):
        assert _parse_date("2024-01-15") == date(2024, 1, 15)

    def test_slash_format(self):
        assert _parse_date("2024/01/15") == date(2024, 1, 15)

    def test_dot_format(self):
        assert _parse_date("2024.01.15") == date(2024, 1, 15)

    def test_short_year(self):
        assert _parse_date("24-01-15") == date(2024, 1, 15)

    def test_invalid_returns_none(self):
        assert _parse_date("not-a-date") is None

    def test_empty_returns_none(self):
        assert _parse_date("") is None


# ─────────────────────────────────────────────
# _parse_amount 테스트
# ─────────────────────────────────────────────

class TestParseAmount:
    def test_plain_integer(self):
        assert _parse_amount("50000") == 50000.0

    def test_comma_separated(self):
        assert _parse_amount("1,234,567") == 1234567.0

    def test_with_won_sign(self):
        assert _parse_amount("50,000원") == 50000.0

    def test_negative_becomes_positive(self):
        assert _parse_amount("-30000") == 30000.0

    def test_zero_returns_none(self):
        assert _parse_amount("0") is None

    def test_invalid_returns_none(self):
        assert _parse_amount("abc") is None


# ─────────────────────────────────────────────
# _normalize_payment_type 테스트
# ─────────────────────────────────────────────

class TestNormalizePaymentType:
    def test_credit_korean(self):
        assert _normalize_payment_type("신용") == PaymentType.CREDIT

    def test_debit_korean(self):
        assert _normalize_payment_type("체크카드") == PaymentType.DEBIT

    def test_transfer_korean(self):
        assert _normalize_payment_type("계좌이체") == PaymentType.TRANSFER

    def test_unknown_returns_none(self):
        assert _normalize_payment_type("알수없음") is None

    def test_case_insensitive(self):
        assert _normalize_payment_type("CREDIT") == PaymentType.CREDIT


# ─────────────────────────────────────────────
# _map_columns 테스트
# ─────────────────────────────────────────────

class TestMapColumns:
    def test_english_headers(self):
        header = ["date", "merchant_name", "amount", "payment_type"]
        mapping = _map_columns(header)
        assert mapping == {
            "date": 0,
            "merchant_name": 1,
            "amount": 2,
            "payment_type": 3,
        }

    def test_korean_headers(self):
        header = ["날짜", "가맹점", "거래금액", "구분"]
        mapping = _map_columns(header)
        assert mapping is not None
        assert set(mapping.keys()) == {"date", "merchant_name", "amount", "payment_type"}

    def test_missing_column_returns_none(self):
        header = ["날짜", "가맹점", "거래금액"]  # payment_type 없음
        mapping = _map_columns(header)
        assert mapping is None


# ─────────────────────────────────────────────
# 전처리기(preprocess) 테스트
# ─────────────────────────────────────────────

def _make_txn(
    merchant: str = "스타벅스",
    amount: float = 5500.0,
    payment_type: PaymentType = PaymentType.CREDIT,
    days_ago: int = 1,
) -> Transaction:
    return Transaction(
        date=date.today() - timedelta(days=days_ago),
        amount=amount,
        merchant_name=merchant,
        payment_type=payment_type,
    )


class TestPreprocessor:
    def test_removes_zero_amount(self):
        txn = _make_txn(amount=0.0)
        result = preprocess(ParseResult(transactions=[txn]))
        assert len(result.transactions) == 0
        assert result.skipped_rows == 1

    def test_removes_future_date(self):
        txn = Transaction(
            date=date.today() + timedelta(days=5),
            amount=10000.0,
            merchant_name="미래가맹점",
            payment_type=PaymentType.CREDIT,
        )
        result = preprocess(ParseResult(transactions=[txn]))
        assert len(result.transactions) == 0
        assert result.skipped_rows == 1

    def test_marks_duplicate_without_removing(self):
        # requirements R3.5-2: 정상 반복 결제가 사라지지 않도록 삭제하지 않고 후보로만 표시
        txn1 = _make_txn()
        txn2 = _make_txn()  # 동일한 거래
        result = preprocess(ParseResult(transactions=[txn1, txn2]))
        assert len(result.transactions) == 2
        assert result.duplicate_indices == [1]
        assert result.skipped_rows == 0

    def test_normalizes_merchant_name(self):
        txn = _make_txn(merchant="  스타벅스!!!  ")
        result = preprocess(ParseResult(transactions=[txn]))
        assert result.transactions[0].merchant_name == "스타벅스"

    def test_empty_merchant_becomes_unknown(self):
        txn = _make_txn(merchant="")
        result = preprocess(ParseResult(transactions=[txn]))
        assert result.transactions[0].merchant_name == "알 수 없음"

    def test_sorted_by_date(self):
        txn1 = _make_txn(merchant="A마트", days_ago=5)
        txn2 = _make_txn(merchant="B카페", days_ago=1)
        txn3 = _make_txn(merchant="C주유소", days_ago=10)
        result = preprocess(ParseResult(transactions=[txn1, txn2, txn3]))
        dates = [t.date for t in result.transactions]
        assert dates == sorted(dates)

    def test_removes_excessive_amount(self):
        txn = _make_txn(amount=2_000_000_000.0)
        result = preprocess(ParseResult(transactions=[txn]))
        assert len(result.transactions) == 0
        assert result.skipped_rows == 1

    def test_preserves_skipped_count_from_parser(self):
        txn = _make_txn()
        result = preprocess(ParseResult(transactions=[txn], skipped_rows=3))
        assert result.skipped_rows == 3  # 파서에서 넘어온 skipped 유지


# ─────────────────────────────────────────────
# pdf_parser.parse_pdf mock 테스트
# ─────────────────────────────────────────────

class TestParsePdf:
    def test_rejects_oversized_file(self):
        from app.pdf_parser import parse_pdf

        oversized = b"x" * (20 * 1024 * 1024 + 1)
        with pytest.raises(PDFParseError, match="20MB"):
            parse_pdf(oversized)

    def test_rejects_invalid_pdf(self):
        from app.pdf_parser import parse_pdf

        with pytest.raises(PDFParseError):
            parse_pdf(b"this is not a pdf")

    def test_rejects_scanned_pdf(self):
        from app.pdf_parser import parse_pdf

        # pdfplumber가 텍스트를 반환하지 않는 경우를 모킹
        mock_page = MagicMock()
        mock_page.extract_text.return_value = None
        mock_page.extract_tables.return_value = []
        mock_pdf = MagicMock()
        mock_pdf.pages = [mock_page]
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)

        with patch("app.pdf_parser.pdfplumber.open", return_value=mock_pdf):
            with pytest.raises(PDFParseError, match="스캔"):
                parse_pdf(b"%PDF-fake")


class TestBankSeparateAmounts:
    def test_shinhan_headers_compact_dates_and_separate_time(self):
        from app.pdf_parser import _extract_transactions_from_table
        header = ['거래일자', '거래시간', '적요', '출금(원)', '입금(원)', '내용', '잔액(원)', '거래점']
        rows = [
            ['20261107', '01:44:57', '체크카드', '15,600', '0', '테스트 상점', '11,272', '지점'],
            ['20261106', '12:20:11', '타행인터넷\n뱅킹', '0', '8,200', '테스트 입금', '26,872', '지점'],
        ]
        txs, skipped = _extract_transactions_from_table(rows, header)
        assert skipped == 0
        assert len(txs) == 2
        assert txs[0].date.isoformat() == '2026-11-07'
        assert txs[0].time == '01:44'
        assert txs[0].amount == 15600
        assert txs[0].direction.value == 'out'
        assert txs[0].payment_type.value == 'debit'
        assert txs[0].merchant_name == '테스트 상점'
        assert txs[1].amount == 8200
        assert txs[1].direction.value == 'in'
        assert txs[1].payment_type.value == 'transfer'
        assert txs[1].time == '12:20'

    def test_query_period_is_not_a_transaction(self):
        from app.pdf_parser import _parse_text_fallback
        txs, skipped = _parse_text_fallback('조회기간 2026.08.08~2026.11.08 대출한도')
        assert txs == []

    def test_header_line_breaks_and_invalid_time(self):
        from app.pdf_parser import _map_columns, _parse_time
        mapping = _map_columns(['거래\n일자', '출금\n(원)', '입금\n(원)', '내용'])
        assert mapping['out_amount'] == 1
        assert mapping['in_amount'] == 2
        assert _parse_time('12:99') is None
