"""
FastAPI 애플리케이션 엔트리포인트

엔드포인트:
- POST /upload/pdf  : PDF 업로드 → 파싱 → 전처리 → CSV 반환
- GET  /health      : 헬스체크
"""

from __future__ import annotations

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import Response, JSONResponse

from app.pdf_parser import PDFParseError
from app.pipeline import PipelineResult, run_pdf_pipeline

app = FastAPI(
    title="AI 가계부 API",
    description="PDF 거래 내역 → CSV 변환 및 전처리 서비스",
    version="0.1.0",
)

# 허용 파일 크기 (FastAPI 레이어에서도 이중 검사)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.post("/upload/pdf")
async def upload_pdf(file: UploadFile = File(...)):
    """
    PDF 거래 내역 파일을 업로드합니다.

    - 텍스트 기반 PDF만 허용 (스캔 이미지 PDF 거부)
    - 최대 파일 크기: 20MB
    - 응답: 전처리 완료된 CSV 문자열 + 분석 요약
    """
    # 파일 확장자 검증
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="PDF 파일(.pdf)만 업로드할 수 있습니다.",
        )

    file_bytes = await file.read()

    # 파일 크기 이중 검사 (Requirement 7 AC #5)
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"파일 크기가 20MB를 초과합니다. "
                f"현재 크기: {len(file_bytes) / 1024 / 1024:.1f}MB"
            ),
        )

    try:
        result: PipelineResult = run_pdf_pipeline(file_bytes)
    except PDFParseError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return JSONResponse(
        content={
            "summary": {
                "total_transactions": result.total_transactions,
                "skipped_rows": result.skipped_rows,
                "warnings": result.warnings,
            },
            "csv": result.csv_string,
        }
    )


@app.post("/upload/pdf/download")
async def upload_pdf_download(file: UploadFile = File(...)):
    """
    PDF를 업로드하면 전처리 완료된 CSV 파일을 직접 다운로드합니다.
    (엑셀 호환 UTF-8 BOM 포함)
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="PDF 파일(.pdf)만 업로드할 수 있습니다.",
        )

    file_bytes = await file.read()

    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"파일 크기가 20MB를 초과합니다.",
        )

    try:
        result: PipelineResult = run_pdf_pipeline(file_bytes)
    except PDFParseError as e:
        raise HTTPException(status_code=422, detail=str(e))

    original_name = file.filename.replace(".pdf", "")
    return Response(
        content=result.csv_bytes,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{original_name}_processed.csv"'
        },
    )
