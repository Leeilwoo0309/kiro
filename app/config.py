"""
설정 값. 환경 변수(.env)에서 읽는다. 키 값은 절대 로그에 남기지 않는다 (R11).

값은 호출 시점에 읽어 테스트에서 환경 변수로 바꿀 수 있게 한다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DEFAULT_LLM_BASE_URL = "https://factchat-cloud.mindlogic.ai/v1/gateway"

# 파일 한도 (R3.5-4). 실행 환경에 맞춰 조정 가능한 설정값
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_CSV_ROWS = 5_000

# 금액 입력 상한 (R2.2)
MAX_AMOUNT_KRW = 999_999_999

# 사용자 월 경계 (Asia/Seoul)
TIMEZONE = "Asia/Seoul"


def data_dir() -> Path:
    return Path(os.getenv("APP_DATA_DIR") or (BASE_DIR / "data"))


def db_path() -> Path:
    return data_dir() / "ledger.sqlite3"


def ai_settings_path() -> Path:
    """웹 화면에서 입력한 AI 키·모델. data/ 아래라 git에 올라가지 않는다. 파일 권한 600."""
    return data_dir() / "ai_settings.json"


def load_ai_settings() -> dict:
    try:
        return json.loads(ai_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_ai_settings(values: dict) -> None:
    path = ai_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(values, f, ensure_ascii=False)
    os.chmod(path, 0o600)


def llm_key_source() -> str:
    if load_ai_settings().get("api_key"):
        return "web"
    return "env" if os.getenv("BAZE_API_KEY", "").strip() else "none"


def llm_api_key() -> str:
    """웹에서 입력한 키가 있으면 그것을, 없으면 .env의 BAZE_API_KEY를 쓴다."""
    return (load_ai_settings().get("api_key") or os.getenv("BAZE_API_KEY", "")).strip()


def llm_base_url() -> str:
    return os.getenv("LLM_BASE_URL", "").strip() or DEFAULT_LLM_BASE_URL


def llm_model() -> str:
    """웹에서 고른 모델이 있으면 그것을, 없으면 .env의 LLM_MODEL을 쓴다."""
    return (load_ai_settings().get("model") or os.getenv("LLM_MODEL", "")).strip()


def llm_timeout_sec() -> float:
    try:
        return float(os.getenv("LLM_TIMEOUT_SEC", "20"))
    except ValueError:
        return 20.0
