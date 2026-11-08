"""
파이프라인 오케스트레이터

PDF 업로드 → 파싱 → 전처리 → CSV 변환의 전체 흐름을 조율합니다.

흐름:
    PDF bytes
        └─▶ pdf_parser.parse_pdf()      → ParseResult (raw)
                └─▶ preprocessor.preprocess()   → ParseResult (clean)
                        └─▶ csv_converter.transactions_to_csv_bytes() → CSV bytes
"""

from __future__ import annotations

from dataclasses import dataclass

from app.csv_converter import transactions_to_csv, transactions_to_csv_bytes
from app.models import ParseResult
from app.pdf_parser import PDFParseError, parse_pdf
from app.preprocessor import preprocess


@dataclass
class PipelineResult:
    """파이프라인 최종 출력"""
    csv_string: str           # 분석 파이프라인(CSV_Parser)으로 전달할 CSV 문자열
    csv_bytes: bytes          # 다운로드용 바이트 (UTF-8 BOM)
    total_transactions: int   # 최종 유효 거래 수
    skipped_rows: int         # 전체 스킵된 행 수 (파싱 + 전처리)
    warnings: list[str]       # 경고 메시지 목록


def run_pdf_pipeline(file_bytes: bytes) -> PipelineResult:
    """
    PDF 바이트를 입력받아 전처리까지 완료된 CSV를 반환합니다.

    Args:
        file_bytes: 업로드된 PDF 파일의 원시 바이트

    Returns:
        PipelineResult

    Raises:
        PDFParseError: 파일 크기 초과 / 스캔 PDF / 파싱 실패
    """
    # 1단계: PDF 파싱
    raw_result: ParseResult = parse_pdf(file_bytes)

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
    )
