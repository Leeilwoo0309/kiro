"""
AI_Adapter: BAZE API Gateway 호출 (R11, R12)

- OpenAI 호환 SDK로 호출한다. 키는 BAZE_API_KEY 환경 변수에서만 읽는다.
- 출력은 JSON으로 받아 Pydantic으로 검증하고, 실패하면 한 번만 보정 재시도한다 (R12.8).
- 로그에는 모델 ID와 토큰 수만 남긴다. 거래 내용, 소명, 키는 남기지 않는다 (R1.6, R11.9).
- JSON 객체 출력 모드를 사용하고 반환값은 Pydantic 스키마로 검증한다.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

from app import config

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_client = None
_client_key: Optional[tuple] = None


class AIUnavailable(Exception):
    """키 없음, 인증 실패, 시간 초과, 한도 초과, 서버 오류, 출력 검증 실패."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def is_configured() -> bool:
    return bool(config.llm_api_key() and config.llm_model())


def status() -> dict:
    """키 값은 돌려주지 않는다. 끝 4자리만 힌트로 준다."""
    key = config.llm_api_key()
    return {
        "configured": is_configured(),
        "has_key": bool(key),
        "key_source": config.llm_key_source(),
        "key_hint": f"…{key[-4:]}" if len(key) >= 8 else None,
        "model": config.llm_model() or None,
        "base_url": config.llm_base_url(),
    }


# 2026-10-27 기준 BAZE 크레딧 단가 (1K 토큰당 입력/출력). 출처: docs.mindlogic.ai 모델별 크레딧 단가
KNOWN_RATES = {
    "gpt-6.1-sol": (2, 10), "gpt-6.1-terra": (1, 5), "gpt-6-astra": (10, 50), "gpt-6-sol": (2, 10),
    "gpt-6-luna": (0.1, 0.5), "gpt-5.6-sol": (4, 20), "gpt-5.6-terra": (2, 12), "gpt-5.6-luna": (0.2, 1.2),
    "gpt-5.5": (5, 30), "claude-fable-5-1": (10, 50), "claude-fable-5": (10, 50), "claude-opus-5-5": (4, 20),
    "claude-opus-5": (5, 25), "claude-opus-4-8": (5, 25), "claude-sonnet-5-5": (2, 10), "claude-sonnet-5": (2, 10),
    "claude-haiku-5-5": (0.1, 0.5), "claude-haiku-4-5-20251001": (1, 5), "gemini-4-argon": (2, 10),
    "gemini-3.1-pro-preview": (2, 12), "gemini-3.8-flash": (0.75, 3.75), "gemini-3.7-flash": (0.75, 3.75),
    "gemini-3.6-flash": (0.75, 3.75), "gemini-3.5-flash": (1.5, 9), "gemini-3.5-flash-lite": (0.3, 2.5),
    "google/gemma-4-31B-it": (0.13, 0.38), "muse-spark-1.3": (1.25, 4.25), "grok-4.7": (2, 6),
    "grok-4.6": (2, 6), "grok-4.5": (2, 6), "grok-4-1-fast": (0.2, 0.5), "solar-pro4": (0.3, 1.2),
    "solar-mini4": (0.1, 0.4), "sonar-pro": (3, 15), "sonar-reasoning-pro": (2, 8),
}


def list_models(api_key: Optional[str] = None) -> list[dict]:
    """Gateway의 대화용(llm) 모델 목록. 키가 틀리면 AIUnavailable('auth')."""
    import httpx

    key = api_key or config.llm_api_key()
    if not key:
        raise AIUnavailable("not_configured")
    try:
        r = httpx.get(config.llm_base_url().rstrip("/") + "/models/",
                      headers={"Authorization": f"Bearer {key}"}, timeout=15)
    except httpx.HTTPError:
        raise AIUnavailable("connection")
    if r.status_code == 401:
        raise AIUnavailable("auth")
    if r.status_code != 200:
        raise AIUnavailable(f"status_{r.status_code}")
    data = r.json()
    items = data.get("data", []) if isinstance(data, dict) else data
    out = []
    for m in items:
        if not isinstance(m, dict) or m.get("type") != "llm" or m.get("lifecycle") == "legacy":
            continue
        if "audio" in str(m.get("id", "")):
            continue
        rate = KNOWN_RATES.get(m["id"])
        out.append({"id": m["id"], "provider": m.get("owned_by"),
                    "input_rate": rate[0] if rate else None, "output_rate": rate[1] if rate else None})
    out.sort(key=lambda x: (x["input_rate"] is None, x["input_rate"] or 0, x["id"]))
    return out


def _get_client():
    global _client, _client_key
    key = (config.llm_base_url(), config.llm_api_key(), config.llm_timeout_sec())
    if _client is None or _client_key != key:
        from openai import OpenAI

        # SDK가 Authorization: Bearer <KEY> 헤더로 보낸다 (R11.3). 재시도는 직접 관리한다.
        _client = OpenAI(base_url=key[0], api_key=key[1], timeout=key[2], max_retries=0)
        _client_key = key
    return _client


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]
    return json.loads(text)


def call_json(system: str, payload: dict, schema: Type[T], max_tokens: int = 700) -> T:
    """시스템 지시와 JSON 입력을 보내고 schema로 검증된 출력을 돌려준다."""
    if not is_configured():
        raise AIUnavailable("not_configured")

    import openai

    client = _get_client()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    last_error = "unknown"
    for attempt in range(2):  # 최초 1회 + 보정 재시도 1회
        try:
            resp = client.chat.completions.create(
                model=config.llm_model(),
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=max_tokens,
            )
        except openai.AuthenticationError:
            log.error("BAZE 401: 키가 잘못되었거나 다른 BAZE 배포에서 발급된 키입니다.")
            raise AIUnavailable("auth")
        except openai.APITimeoutError:
            raise AIUnavailable("timeout")
        except openai.RateLimitError:
            raise AIUnavailable("rate_limited")
        except openai.APIConnectionError:
            raise AIUnavailable("connection")
        except openai.APIStatusError as e:
            raise AIUnavailable(f"status_{e.status_code}")

        usage = getattr(resp, "usage", None)
        log.info(
            "llm_call model=%s prompt_tokens=%s completion_tokens=%s attempt=%d",
            config.llm_model(),
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
            attempt + 1,
        )
        content = resp.choices[0].message.content if resp.choices else ""
        try:
            return schema.model_validate(_extract_json(content or ""))
        except (ValueError, ValidationError) as e:
            last_error = type(e).__name__
            messages.append({"role": "assistant", "content": content or ""})
            messages.append({
                "role": "user",
                "content": "출력이 요구한 JSON 형식과 맞지 않습니다. 설명 없이 형식에 맞는 JSON 하나만 다시 출력하세요.",
            })
    raise AIUnavailable(f"invalid_output:{last_error}")
