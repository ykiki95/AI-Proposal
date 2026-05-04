"""
Anthropic Claude 클라이언트 (v2).

기본은 Anthropic API 직접 호출. SYS.use_bedrock=True일 때 AnthropicBedrock으로 분기.
프롬프트 캐싱(ephemeral, 5분 TTL)은 직접 호출 모드에서만 적용 (Bedrock은 silent disable).
"""

from __future__ import annotations

import os
from collections import defaultdict
from functools import lru_cache
from threading import Lock
from typing import Dict, List, Optional, Union

from anthropic import Anthropic, AnthropicBedrock
from loguru import logger

from config.settings import KEYS, SYS


# ---------------------------------------------------------------------------
# Bedrock 모델 ID 매핑
# ---------------------------------------------------------------------------
# Anthropic 직접 호출 ID → AWS Bedrock inference profile ID.
#
# 2026-05 현재 Bedrock에는 Claude 4.7/4.6 cross-region inference profile이 어떤
# 리전에도 미배포된 상태이므로 도쿄 리전 + 4.5 시리즈로 다운그레이드 매핑.
#   - Opus 4.7   → Sonnet 4.5 (jp.)
#   - Sonnet 4.6 → Sonnet 4.5 (jp.)
#   - Haiku 4.5  → Haiku 4.5 (jp.) (동일 버전 유지)
#
# 향후 4.7/4.6 inference profile이 배포되면 .env의 BEDROCK_MODEL_* 3줄만 변경해 즉시 전환 가능.
# Anthropic 결제 풀려 직결로 복귀할 때는 .env에서 USE_BEDROCK=false 한 줄만 변경하면
# settings.py의 모델명(opus-4-7/sonnet-4-6) 그대로 동작.

_BEDROCK_MODEL_MAP: Dict[str, str] = {
    "claude-opus-4-7": os.environ.get(
        "BEDROCK_MODEL_OPUS", "jp.anthropic.claude-sonnet-4-5-20250929-v1:0"
    ),
    "claude-sonnet-4-6": os.environ.get(
        "BEDROCK_MODEL_SONNET", "jp.anthropic.claude-sonnet-4-5-20250929-v1:0"
    ),
    "claude-haiku-4-5-20251001": os.environ.get(
        "BEDROCK_MODEL_HAIKU", "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
    ),
}


SystemParam = Union[str, List[Dict]]
UserParam = Union[str, List[Dict]]


# ---------------------------------------------------------------------------
# 누적 토큰 사용량 추적 (프로세스 라이프타임)
# ---------------------------------------------------------------------------
# 모델별로 in/out/cache_read/cache_create/calls를 누적한다.
# 한 번의 batch 실행 후 get_usage_summary()로 총 비용을 확인할 수 있도록.

_USAGE_LOCK = Lock()
_USAGE_TRACKER: Dict[str, Dict[str, int]] = defaultdict(
    lambda: {"in": 0, "out": 0, "cache_read": 0, "cache_create": 0, "calls": 0}
)


def get_usage_summary() -> Dict[str, Dict[str, int]]:
    """현재 프로세스의 누적 토큰 사용량을 모델별로 반환."""
    with _USAGE_LOCK:
        return {k: dict(v) for k, v in _USAGE_TRACKER.items()}


def reset_usage() -> None:
    """누적 카운터를 0으로 초기화. 단계별 비용 측정 시 사용."""
    with _USAGE_LOCK:
        _USAGE_TRACKER.clear()


@lru_cache(maxsize=1)
def get_anthropic_client() -> Optional[Union[Anthropic, AnthropicBedrock]]:
    """LLM 클라이언트 싱글턴.

    SYS.use_bedrock=True + AWS 키 3종 모두 있을 때 AnthropicBedrock 반환.
    그 외에는 Anthropic 직접 호출. (함수명은 backward-compat 위해 유지.)
    """
    if SYS.use_bedrock:
        if KEYS.has_bedrock:
            logger.info(f"[llm_client] Bedrock 모드 (region={KEYS.aws_region})")
            return AnthropicBedrock(
                aws_access_key=KEYS.aws_access_key_id,
                aws_secret_key=KEYS.aws_secret_access_key,
                aws_region=KEYS.aws_region,
            )
        logger.warning(
            "USE_BEDROCK=true이지만 AWS 키 누락 — Anthropic 직접 호출로 fallback"
        )
    if not KEYS.has_anthropic:
        logger.warning("ANTHROPIC_API_KEY 미설정 — LLM 호출이 실패합니다.")
        return None
    return Anthropic(api_key=KEYS.anthropic_api_key)


def chat(
    model: str,
    system: SystemParam,
    user: UserParam,
    max_tokens: int = 4096,
    cache_system: bool = False,
    temperature: Optional[float] = None,
) -> str:
    """Anthropic Messages API 단일 턴 호출 래퍼.

    Args:
        model: Claude 모델 ID (예: "claude-opus-4-7").
        system: system 프롬프트 (문자열 또는 content block 리스트).
        user: user 메시지 (문자열 또는 content block 리스트).
        max_tokens: 최대 출력 토큰.
        cache_system: True 시 system 프롬프트에 ephemeral cache_control 부여.
            5분 TTL 동안 동일 prefix 재사용 시 입력 토큰 단가가 캐시 읽기 가격으로 감소.
            >1024 토큰의 정적 system 프롬프트에서 의미 있는 효과.
        temperature: 모델 temperature. None이면 SDK 기본값 사용.

    Returns:
        모델 응답 텍스트(여러 text block은 줄바꿈으로 결합).
    """
    client = get_anthropic_client()
    if client is None:
        raise RuntimeError(
            "LLM 클라이언트 미설정. USE_BEDROCK=true이면 AWS 키 3종, "
            "아니면 ANTHROPIC_API_KEY가 .env에 필요합니다."
        )

    # Bedrock 모드: 호환성 위해 cache_control 비활성화, 모델 ID 변환
    bedrock_active = SYS.use_bedrock and KEYS.has_bedrock
    if bedrock_active and cache_system:
        logger.debug("[bedrock] cache_system disabled (호환성)")
        cache_system = False
    effective_model = _BEDROCK_MODEL_MAP.get(model, model) if bedrock_active else model

    if isinstance(system, str):
        system_param: SystemParam = (
            [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            if cache_system
            else system
        )
    else:
        system_param = system

    messages = [{"role": "user", "content": user}]

    kwargs = {
        "model": effective_model,
        "max_tokens": max_tokens,
        "system": system_param,
        "messages": messages,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature

    msg = client.messages.create(**kwargs)

    usage = getattr(msg, "usage", None)
    if usage is not None:
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
        with _USAGE_LOCK:
            u = _USAGE_TRACKER[model]
            u["in"] += usage.input_tokens
            u["out"] += usage.output_tokens
            u["cache_read"] += cache_read
            u["cache_create"] += cache_create
            u["calls"] += 1
        logger.debug(
            f"[anthropic:{model}] in={usage.input_tokens} out={usage.output_tokens} "
            f"cache_read={cache_read} cache_create={cache_create}"
        )

    parts = []
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


# v1 호환 별칭 — 기존 caller가 점진 마이그레이션할 수 있도록 유지.
chat_anthropic = chat
