"""
LLM 클라이언트 팩토리.
Replit AI Integrations(자동 주입된 base_url + api_key)를 사용해 OpenAI/Anthropic SDK를 초기화한다.
별도 API 키 발급 없이 동작.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from anthropic import Anthropic
from loguru import logger
from openai import OpenAI

from config.settings import LLM


@lru_cache(maxsize=1)
def get_openai_client() -> Optional[OpenAI]:
    """Replit AI Integrations 경유 OpenAI 클라이언트."""
    if not LLM.has_openai:
        logger.warning("OpenAI 통합 환경변수가 없습니다. OpenAI 호출이 실패합니다.")
        return None
    return OpenAI(base_url=LLM.openai_base_url, api_key=LLM.openai_api_key)


@lru_cache(maxsize=1)
def get_anthropic_client() -> Optional[Anthropic]:
    """Replit AI Integrations 경유 Anthropic 클라이언트."""
    if not LLM.has_anthropic:
        logger.warning("Anthropic 통합 환경변수가 없습니다. Anthropic 호출이 실패합니다.")
        return None
    return Anthropic(base_url=LLM.anthropic_base_url, api_key=LLM.anthropic_api_key)


# ---------------------------------------------------------------------------
# 공통 호출 래퍼 - 비용 로깅과 단순화된 인터페이스
# ---------------------------------------------------------------------------

def chat_anthropic(model: str, system: str, user: str, max_tokens: int = 4096) -> str:
    """Anthropic Messages API 단순 래퍼."""
    client = get_anthropic_client()
    if client is None:
        raise RuntimeError("Anthropic 클라이언트가 구성되지 않았습니다.")

    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    # 토큰 로깅
    try:
        usage = getattr(msg, "usage", None)
        if usage is not None:
            logger.debug(
                f"[anthropic:{model}] in={usage.input_tokens} out={usage.output_tokens}"
            )
    except Exception:
        pass

    parts = []
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def chat_openai(model: str, system: str, user: str, max_tokens: int = 4096) -> str:
    """OpenAI Chat Completions 단순 래퍼."""
    client = get_openai_client()
    if client is None:
        raise RuntimeError("OpenAI 클라이언트가 구성되지 않았습니다.")

    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    try:
        usage = getattr(resp, "usage", None)
        if usage is not None:
            logger.debug(
                f"[openai:{model}] in={usage.prompt_tokens} out={usage.completion_tokens}"
            )
    except Exception:
        pass
    return (resp.choices[0].message.content or "").strip()
