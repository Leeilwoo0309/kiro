"""API 오류. main.py의 예외 처리기가 {"error": {code, message, details}} 형식으로 바꾼다."""

from __future__ import annotations

from typing import Any, Optional


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Optional[Any] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def bad_request(message: str, details: Any = None) -> AppError:
    return AppError(400, "bad_request", message, details)


def not_found(message: str = "대상을 찾을 수 없습니다.") -> AppError:
    return AppError(404, "not_found", message)


def conflict(message: str, details: Any = None) -> AppError:
    return AppError(409, "conflict", message, details)


def unprocessable(message: str, details: Any = None) -> AppError:
    return AppError(422, "unprocessable", message, details)
