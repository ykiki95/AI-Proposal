"""
RFP 본문에서 '실제 제안서 목차 트리'를 LLM 1회 호출로 추출.

기본 8명 SPECIALISTS는 일반 SI 가정에 맞춰져 있으나, 발주처가 요구하는
제안서 목차(예: TOPIK '제안요청서' 6장 또는 G2B 표준 6항목)는 사업마다 다르다.
박팀장이 plan_toc에 들어가기 전에 이 추출기를 호출하면, RFP 본문에 등장하는
실제 목차를 그대로 따르되, 각 챕터를 8명 base specialty 중 하나로 매핑해 작성을 위임할 수 있다.

출력 schema (list[dict]):
[
  {
    "title": "Ⅰ. 사업 이해",
    "specialty": "business",          # 8명 base key 중 하나
    "display_role": "사업이해 분석가", # 동적 호칭
    "target_pages": 6,
    "brief": "RFP에서 추출한 핵심 작성 지시 2~5문장",
    "sub_chapters": [
      {"title": "1. 사업 개요", "target_pages": 1, "brief": "..."},
      {"title": "2. 추진 배경", "target_pages": 3, "brief": "..."}
    ]
  },
  ...
]

추가로 글로벌 스토리 컨텍스트도 같은 호출에서 산출:
{
  "vision": "한 줄 비전",
  "key_keywords": ["키워드1", ..., "키워드5"],
  "differentiators": ["차별점1", "차별점2", "차별점3"],
  "transitions": ["챕터1→챕터2 한 줄 연결문장", ...]
}
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple

from loguru import logger

from config.settings import MODELS
from tools.llm_clients import chat_anthropic


# 8명 base specialty key (park_team SPECIALISTS와 동기화)
BASE_SPECIALTIES = [
    "business",      # 사업 배경/목적/As-Is·To-Be
    "solution",      # 시스템 구성도/기술/화면
    "methodology",   # 구축 방법론/프로세스
    "schedule",      # 일정/WBS/리스크
    "organization",  # 인력/조직 (사람 작성)
    "company",       # 회사/실적 (사람 작성)
    "quality",       # 품질/보안/유지보수
    "cost",          # 비용/효과
]

VALID_SPECIALTIES = set(BASE_SPECIALTIES)


SYSTEM_PROMPT = """당신은 한국 공공·민간 SI 제안서를 100건 이상 만들어 본 베테랑 제안 팀장입니다.
RFP(제안요청서)를 읽고 발주처가 요구하는 '제안서 목차'를 그대로 추출한 뒤,
각 챕터를 우리 회사 8명 전문가 중 한 명에게 매핑하는 작업을 맡았습니다.

원칙:
- 발주처가 RFP에 명시한 목차(예: "Ⅰ. 사업 이해 / Ⅱ. 사업 추진방향 / ...")를 최우선으로 따른다.
- 명시된 목차가 없으면 제안서 표준 6장 구성(사업이해/제안내용/방법론/조직·인력/품질·보안/회사·실적·비용)을 가정한다.
- 한 챕터가 너무 크면(목표 10페이지 이상) sub_chapters로 2~5개로 쪼갠다. 그래야 LLM 한 번 호출로 작성 가능.
- 각 챕터는 반드시 8명 base specialty 중 하나로 매핑한다(다중 매핑 금지).
- target_pages 합계는 70~120페이지 범위가 되도록 배분한다.
- brief는 RFP 본문 표현을 짧게 인용하여 평가위원이 읽었을 때 "RFP 그대로 반영했네" 라고 느끼게 한다.
"""


def _user_prompt(bid) -> str:
    rfp_text = (bid.rfp_full_text or bid.rfp_summary or "").strip()
    spec_lines = "\n".join(f"- `{k}`" for k in BASE_SPECIALTIES)
    return f"""# 공고 정보
- 사업명: {bid.title}
- 발주기관: {bid.agency}
- 예산: {(bid.budget_krw or 0):,}원
- 사업기간: {bid.duration_months or '미상'}개월

# RFP 본문 (앞부분 발췌)
{rfp_text[:18000]}

# 사용 가능한 8명 base specialty (반드시 이 키 중 하나로 매핑)
{spec_lines}

# 출력 형식 — 오직 JSON 객체 하나만, 다른 텍스트 금지, 마크다운 코드블록 금지
{{
  "vision": "한 줄 비전(50자 내외) — 이 사업이 끝나면 발주처가 갖게 될 미래 모습",
  "key_keywords": ["키워드1", "키워드2", "키워드3", "키워드4", "키워드5"],
  "differentiators": ["우리 회사만의 차별점1", "차별점2", "차별점3"],
  "chapters": [
    {{
      "title": "Ⅰ. 사업 이해",
      "specialty": "business",
      "display_role": "사업이해 분석가",
      "target_pages": 8,
      "brief": "RFP의 추진 배경·정책 근거·핵심 페인포인트를 짚고 발주처가 진짜 원하는 변화를 명시한다. ('보안 강화/품질 안정/통합 운영' 3개 축 강조)",
      "sub_chapters": [
        {{"title": "1. 사업 개요", "target_pages": 2, "brief": "..."}},
        {{"title": "2. 추진 배경", "target_pages": 3, "brief": "..."}},
        {{"title": "3. 사업 목적", "target_pages": 3, "brief": "..."}}
      ]
    }}
  ],
  "transitions": [
    "챕터1→챕터2 한 줄 연결문장",
    "챕터2→챕터3 한 줄 연결문장"
  ]
}}

규칙:
1. RFP에 '제안서 목차' 또는 '목차 작성 방법'이 명시되어 있으면, 그 목차를 100% 그대로 사용한다.
2. 한 챕터가 6페이지 이상이면 sub_chapters로 2개 이상 분할 (LLM 1회 출력 한계 회피).
3. specialty는 반드시 8개 키 중 하나. organization/company는 사람이 직접 채우는 영역으로 배정.
4. 모든 한국어. 'I/II'가 아닌 'Ⅰ/Ⅱ' (로마숫자) 표기 우선.
5. transitions는 챕터 개수 - 1 만큼.
6. JSON만 출력. 설명·인사·코드블록 금지.
"""


def _extract_first_json_object(text: str) -> Optional[str]:
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


def _normalize_chapter(ch: dict) -> Optional[dict]:
    """LLM 출력 챕터 dict를 정규화. 실패 시 None."""
    if not isinstance(ch, dict):
        return None
    title = (ch.get("title") or "").strip()
    if not title:
        return None
    specialty = (ch.get("specialty") or "business").strip()
    if specialty not in VALID_SPECIALTIES:
        # 추측 매핑
        t = title.lower()
        if any(k in title for k in ("기술", "솔루션", "구축내용", "제안내용", "시스템", "아키텍처", "기능")):
            specialty = "solution"
        elif any(k in title for k in ("일정", "WBS", "스케줄", "마일")):
            specialty = "schedule"
        elif any(k in title for k in ("방법론", "절차", "프로세스", "수행 방안")):
            specialty = "methodology"
        elif any(k in title for k in ("품질", "보안", "유지보수", "운영")):
            specialty = "quality"
        elif any(k in title for k in ("조직", "인력", "PM", "투입")):
            specialty = "organization"
        elif any(k in title for k in ("회사", "실적", "회사소개", "수행실적")):
            specialty = "company"
        elif any(k in title for k in ("비용", "산정", "가격", "효과", "투자")):
            specialty = "cost"
        else:
            specialty = "business"
    display_role = (ch.get("display_role") or "").strip() or f"{title} 담당"
    try:
        target_pages = max(1, int(ch.get("target_pages") or 6))
    except Exception:
        target_pages = 6
    brief = (ch.get("brief") or "").strip()

    sub_raw = ch.get("sub_chapters") or []
    subs: List[dict] = []
    if isinstance(sub_raw, list):
        for sc in sub_raw[:8]:
            if not isinstance(sc, dict):
                continue
            stitle = (sc.get("title") or "").strip()
            if not stitle:
                continue
            try:
                sp = max(1, int(sc.get("target_pages") or 2))
            except Exception:
                sp = 2
            subs.append({
                "title": stitle,
                "target_pages": sp,
                "brief": (sc.get("brief") or "").strip(),
            })
    # 챕터가 큰데 sub가 없으면 자동 분할
    if not subs and target_pages >= 7:
        # 3등분
        third = max(2, target_pages // 3)
        subs = [
            {"title": f"{title} (1/3)", "target_pages": third, "brief": brief},
            {"title": f"{title} (2/3)", "target_pages": third, "brief": brief},
            {"title": f"{title} (3/3)", "target_pages": target_pages - third * 2,
             "brief": brief},
        ]
    return {
        "title": title,
        "specialty": specialty,
        "display_role": display_role,
        "target_pages": target_pages,
        "brief": brief,
        "sub_chapters": subs,
    }


def extract_rfp_toc(bid) -> Tuple[List[dict], dict]:
    """RFP 본문 → (chapter list, global_context).

    실패 시 ([], {}) 반환. 호출측은 이 경우 기존 8명 baseline TOC를 사용한다.
    """
    rfp_text = (bid.rfp_full_text or bid.rfp_summary or "").strip()
    if not rfp_text:
        logger.info("[rfp_toc_extractor] RFP 본문 비어있음 — 추출 생략")
        return [], {}

    try:
        raw = chat_anthropic(
            MODELS.park,
            SYSTEM_PROMPT,
            _user_prompt(bid),
            max_tokens=6000,
        )
    except Exception as e:
        logger.warning(f"[rfp_toc_extractor] LLM 호출 실패: {e}")
        return [], {}

    json_text = _extract_first_json_object(raw)
    if not json_text:
        logger.warning("[rfp_toc_extractor] JSON 객체를 찾지 못함")
        return [], {}

    try:
        data = json.loads(json_text)
    except Exception as e:
        logger.warning(f"[rfp_toc_extractor] JSON 파싱 실패: {e}")
        return [], {}

    if not isinstance(data, dict):
        return [], {}

    chapters_raw = data.get("chapters") or []
    chapters: List[dict] = []
    for ch in chapters_raw if isinstance(chapters_raw, list) else []:
        n = _normalize_chapter(ch)
        if n:
            chapters.append(n)
    if not chapters:
        logger.warning("[rfp_toc_extractor] 추출된 챕터 0건")
        return [], {}

    # 글로벌 컨텍스트
    global_ctx = {
        "vision": (data.get("vision") or "").strip()[:200],
        "key_keywords": [
            str(k).strip() for k in (data.get("key_keywords") or [])
            if str(k).strip()
        ][:8],
        "differentiators": [
            str(d).strip() for d in (data.get("differentiators") or [])
            if str(d).strip()
        ][:5],
        "transitions": [
            str(t).strip() for t in (data.get("transitions") or [])
            if str(t).strip()
        ][: max(0, len(chapters) - 1)],
    }
    total_pages = sum(c["target_pages"] for c in chapters)
    logger.info(
        f"[rfp_toc_extractor] {len(chapters)}챕터 추출 (총 {total_pages}p, "
        f"sub {sum(len(c['sub_chapters']) for c in chapters)}개)"
    )
    return chapters, global_ctx
