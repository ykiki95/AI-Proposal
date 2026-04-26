"""
RFP 본문에서 8명 전문가별 작성 가이드(brief)를 자동 추출.
박제안 팀장의 plan_toc 단계에서 1회 LLM 호출로 사용된다.

출력은 specialty key → brief(2~5줄) 매핑.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List

from loguru import logger

from config.settings import MODELS
from tools.llm_clients import chat_anthropic


SPECIALTY_DESCRIPTIONS: List[Dict[str, str]] = [
    {"key": "business",     "role": "사업 배경/목적/As-Is·To-Be",
     "focus": "RFP가 말하는 추진 배경·정책 근거·핵심 페인포인트, 발주처가 진짜 원하는 변화"},
    {"key": "solution",     "role": "시스템 구성도/기술 스택/To-Be 화면",
     "focus": "RFP가 요구하는 기능·외부 연계·보안·성능·인프라 제약, 채택해야 할 기술 키워드"},
    {"key": "methodology",  "role": "구축 방법론/품질 관리",
     "focus": "RFP가 명시한 추진 절차·산출물·검수 기준·품질 표준(예: ISO/CMMI/PMBOK 인용 여부)"},
    {"key": "schedule",     "role": "일정/마일스톤/WBS",
     "focus": "사업기간, 단계 구분(분석/설계/개발/시험/이행), 검수 시점, 리스크 일정"},
    {"key": "organization", "role": "투입 인력/조직",
     "focus": "RFP가 요구하는 PM/PL 자격, 등급별 인력 수, 보유 자격증, 파견·상주 조건 (사람 작성용 가이드)"},
    {"key": "company",      "role": "회사 소개/유사 실적",
     "focus": "RFP가 강조하는 실적 요건·수행 경험·인증 (사람 작성용 가이드)"},
    {"key": "quality",      "role": "품질/보안/유지보수/위험관리",
     "focus": "보안 요건(개인정보·암호화·인증 등급), 품질 KPI, 운영·유지보수 요구사항, 위험관리 항목"},
    {"key": "cost",         "role": "비용/투자효과/ROI",
     "focus": "예산 규모, 산정 기준, 정량 효과(절감액·시간 단축·서비스 확대), 발주처가 평가에서 보는 가성비 포인트"},
]


SYSTEM_PROMPT = """당신은 한국 공공·민간 SI 제안서를 100건 이상 만들어 본 베테랑 제안 팀장입니다.
RFP 원문을 읽고, 제안서 8개 섹션을 맡을 각 전문가가 '이 RFP에 한해 반드시 강조해야 할 작성 지시문'을 만들어 줍니다.
- 추측 금지. RFP에 명시되지 않은 항목은 '명시 없음, 일반 기준 적용' 식으로 표현.
- RFP에서 발견한 평가 기준·필수 요건·금지 사항·발주처가 강조한 키워드를 인용하라.
- 각 전문가 brief는 2~5문장(150~400자)으로 짧고 실행 가능하게 작성.
"""


def _user_prompt(bid) -> str:
    spec_lines = "\n".join(
        f"- `{s['key']}` ({s['role']}): 주목할 부분 → {s['focus']}"
        for s in SPECIALTY_DESCRIPTIONS
    )
    rfp_text = (bid.rfp_full_text or bid.rfp_summary or "").strip()
    return f"""# 공고 정보
- 사업명: {bid.title}
- 발주기관: {bid.agency}
- 예산: {(bid.budget_krw or 0):,}원
- 사업기간: {bid.duration_months or '미상'}개월

# RFP 본문
{rfp_text[:14000]}

# 8명 전문가
{spec_lines}

# 출력 형식 (오직 JSON 객체 하나, 다른 텍스트 절대 금지)
{{
  "business":     "이 RFP에 한정한 작성 지시 2~5문장",
  "solution":     "...",
  "methodology":  "...",
  "schedule":     "...",
  "organization": "...",
  "company":      "...",
  "quality":      "...",
  "cost":         "..."
}}

규칙:
1. 8개 키 모두 채울 것 (RFP에 정보가 없으면 "RFP 명시 없음 — 일반 기준으로 작성"으로 명시).
2. 키워드·요건은 가능하면 RFP 본문 표현을 짧게 인용 (예: '"개인정보 영향평가 필수" 명시').
3. 본문 작성 지시이지, 본문 자체가 아니다. 한 단락(2~5문장)으로 압축할 것.
"""


def _extract_first_json_object(text: str) -> str | None:
    """문자열에서 첫 JSON 객체를 brace-count 방식으로 추출."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


VALID_KEYS = {s["key"] for s in SPECIALTY_DESCRIPTIONS}


def extract_section_guides(bid) -> Dict[str, str]:
    """RFP를 LLM 1회 호출로 분석해 specialty key → brief 매핑 반환.

    실패 시 빈 dict 반환 (호출측에서 빈 brief 그대로 사용).
    """
    rfp_text = (bid.rfp_full_text or bid.rfp_summary or "").strip()
    if not rfp_text:
        logger.info("[rfp_section_guide] RFP 본문 비어있음 — 가이드 추출 생략")
        return {}

    try:
        raw = chat_anthropic(
            MODELS.park,
            SYSTEM_PROMPT,
            _user_prompt(bid),
            max_tokens=2500,
        )
    except Exception as e:
        logger.warning(f"[rfp_section_guide] LLM 호출 실패: {e}")
        return {}

    json_text = _extract_first_json_object(raw)
    if not json_text:
        logger.warning("[rfp_section_guide] JSON 객체를 찾지 못함 — 가이드 비움")
        return {}

    try:
        data = json.loads(json_text)
    except Exception as e:
        logger.warning(f"[rfp_section_guide] JSON 파싱 실패: {e}")
        return {}

    if not isinstance(data, dict):
        return {}

    out: Dict[str, str] = {}
    for k, v in data.items():
        if k in VALID_KEYS and isinstance(v, str):
            text = v.strip()
            # 너무 긴 brief는 600자로 절단
            if len(text) > 600:
                text = text[:600].rstrip() + "…"
            if text:
                out[k] = text
    logger.info(f"[rfp_section_guide] 가이드 {len(out)}/8 추출 완료")
    return out
