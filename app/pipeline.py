"""
파이프라인 오케스트레이터

PDF 업로드 → 파싱 → 전처리 → CSV 변환의 전체 흐름을 조율합니다.

흐름:
    PDF bytes
        └─▶ pdf_parser.parse_pdf()      → ParseResult (raw)
                └─▶ preprocessor.preprocess()   → ParseResult (clean, 중복 후보 표시)
                        └─▶ csv_converter.transactions_to_csv_bytes() → CSV bytes
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.csv_converter import transactions_to_csv, transactions_to_csv_bytes
from app.models import ParseResult, Transaction
from app.pdf_parser import PDFParseError, parse_pdf
from app.preprocessor import preprocess


@dataclass
class PipelineResult:
    """파이프라인 최종 출력"""
    csv_string: str           # 표준 거래 CSV 문자열
    csv_bytes: bytes          # 다운로드용 바이트 (UTF-8 BOM)
    total_transactions: int   # 최종 유효 거래 수
    skipped_rows: int         # 전체 스킵된 행 수 (파싱 + 전처리)
    warnings: list[str]       # 경고 메시지 목록
    transactions: list[Transaction] = field(default_factory=list)
    duplicate_indices: list[int] = field(default_factory=list)


def run_pdf_pipeline(file_bytes: bytes, password: Optional[str] = None) -> PipelineResult:
    """
    PDF 바이트를 입력받아 전처리까지 완료된 CSV를 반환합니다.

    Raises:
        PDFParseError: 파일 크기 초과 / 스캔 PDF / 비밀번호 오류 / 파싱 실패
    """
    # 1단계: PDF 파싱
    raw_result: ParseResult = parse_pdf(file_bytes, password=password)

    # 2단계: 전처리
    clean_result: ParseResult = preprocess(raw_result)

    # 3단계: CSV 변환
    csv_string = transactions_to_csv(clean_result.transactions)
    csv_bytes = transactions_to_csv_bytes(clean_result.transactions)

    return PipelineResult(
        csv_string=csv_string,
        csv_bytes=csv_bytes,
        total_transactions=len(clean_result.transactions),
        skipped_rows=clean_result.skipped_rows,
        warnings=clean_result.warnings,
        transactions=clean_result.transactions,
        duplicate_indices=clean_result.duplicate_indices,
    )
