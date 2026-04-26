"""
한국어 검수 도구.
- hanspell이 설치되어 있으면 우선 사용
- 미설치/오류 시 LLM 기반 검수로 fallback
"""

from __future__ import annotations

from typing import Dict, List

from loguru import logger

try:
    from hanspell import spell_checker  # type: ignore
    _HANSPELL_OK = True
except Exception:
    _HANSPELL_OK = False


def check_spelling(text: str) -> List[Dict]:
    """텍스트의 맞춤법 오류를 [{position, original, suggestion}] 형태로 반환."""
    if _HANSPELL_OK:
        try:
            issues: List[Dict] = []
            # 한 번에 너무 길면 나눠서 처리
            for chunk in _chunks(text, 400):
                result = spell_checker.check(chunk)
                for word, err in result.words.items():
                    if err != 0:  # 0=정상, 그 외=오류 코드
                        issues.append({"original": word, "suggestion": "(자동제안)"})
            return issues
        except Exception as e:
            logger.warning(f"hanspell 실패, LLM fallback: {e}")

    return _llm_fallback(text)


def _chunks(s: str, size: int) -> List[str]:
    return [s[i:i + size] for i in range(0, len(s), size)]


def _llm_fallback(text: str) -> List[Dict]:
    """LLM 기반 맞춤법 검사 (간이). 호출 실패 시 빈 리스트."""
    from tools.llm_clients import chat_openai
    from config.settings import MODELS

    prompt = (
        "다음 한국어 문서에서 맞춤법/띄어쓰기 오류를 최대 10개까지 찾아 "
        "JSON 배열로 반환하세요. 각 항목은 {original, suggestion} 형식입니다. "
        "오류가 없으면 빈 배열을 반환하세요. 설명은 출력하지 마세요.\n\n"
        f"---\n{text[:3000]}\n---"
    )
    try:
        out = chat_openai(MODELS.oh, "당신은 한국어 교정 전문가입니다.", prompt, max_tokens=1024)
        import json, re
        m = re.search(r"\[.*\]", out, re.S)
        if m:
            data = json.loads(m.group(0))
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.debug(f"LLM 검사 실패: {e}")
    return []
