"""
Voyage AI 임베딩 모듈.
- voyage-multilingual-2 모델 사용
- 배치 처리 + tenacity 재시도
- 텍스트 임베딩 / 쿼리 임베딩 분리 지원
"""

from __future__ import annotations

import time
from typing import Any

from loguru import logger

try:
    import voyageai
    _VOYAGE_AVAILABLE = True
except ImportError:
    _VOYAGE_AVAILABLE = False

try:
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
    _TENACITY_AVAILABLE = True
except ImportError:
    _TENACITY_AVAILABLE = False


def _get_client(api_key: str | None = None) -> Any:
    if not _VOYAGE_AVAILABLE:
        raise ImportError("voyageai 미설치. pip install voyageai")
    if api_key is None:
        from config.settings import KEYS
        api_key = KEYS.voyage_api_key
    if not api_key:
        raise ValueError("VOYAGE_API_KEY가 설정되지 않았습니다.")
    return voyageai.Client(api_key=api_key)


def _embed_batch_raw(
    client: Any,
    texts: list[str],
    model: str,
    input_type: str,
) -> list[list[float]]:
    """실제 Voyage API 호출."""
    result = client.embed(texts, model=model, input_type=input_type)
    return result.embeddings


def embed_documents(
    texts: list[str],
    model: str | None = None,
    batch_size: int = 32,
    api_key: str | None = None,
) -> list[list[float]]:
    """문서 텍스트 배치 임베딩. 빈 텍스트 처리 포함."""
    if not texts:
        return []

    client = _get_client(api_key)
    if model is None:
        from config.settings import VECTOR
        model = VECTOR.voyage_model

    # 빈 텍스트 자리 표시
    valid_indices = [i for i, t in enumerate(texts) if t and t.strip()]
    valid_texts = [texts[i] for i in valid_indices]

    if not valid_texts:
        return [[] for _ in texts]

    all_embeddings: list[list[float]] = []
    total = len(valid_texts)

    for start in range(0, total, batch_size):
        batch = valid_texts[start : start + batch_size]
        batch_num = start // batch_size + 1
        total_batches = (total + batch_size - 1) // batch_size
        logger.debug(f"임베딩 배치 {batch_num}/{total_batches} ({len(batch)}건)")

        for attempt in range(3):
            try:
                embeddings = _embed_batch_raw(client, batch, model, "document")
                all_embeddings.extend(embeddings)
                break
            except Exception as e:
                if attempt == 2:
                    logger.error(f"임베딩 실패 (배치 {batch_num}): {e}")
                    raise
                wait = 2 ** attempt
                logger.warning(f"임베딩 재시도 {attempt + 1}/3 ({wait}초 대기): {e}")
                time.sleep(wait)

        # Rate limit 방지
        if start + batch_size < total:
            time.sleep(0.3)

    # 빈 텍스트 위치에 빈 벡터 삽입
    result: list[list[float]] = [[] for _ in texts]
    for out_idx, orig_idx in enumerate(valid_indices):
        result[orig_idx] = all_embeddings[out_idx]

    return result


def embed_query(
    query: str,
    model: str | None = None,
    api_key: str | None = None,
) -> list[float]:
    """단일 검색 쿼리 임베딩 (input_type=query)."""
    client = _get_client(api_key)
    if model is None:
        from config.settings import VECTOR
        model = VECTOR.voyage_model

    result = client.embed([query], model=model, input_type="query")
    return result.embeddings[0]
