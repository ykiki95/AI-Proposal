"""
StrategyAgent — RFP 본문을 구조화 + win 전략 도출.

DiscoveryAgent가 채워둔 BidNotice.rfp_full_text를 입력으로 받아 RfpStructured를 산출한다.
산출물은 WriterAgent의 작성 가이드, ReviewerAgent의 수용표 검증, GraphicsAgent의 슬라이드 설계
모두의 공통 입력으로 사용된다.

3개 LLM 호출 구조:
  1. TOC 추출         — tools.rfp_toc_extractor.extract_rfp_toc (재사용)
  2. 요구사항 + 평가기준 — Opus 1회 (system 캐시)
  3. 전략 메모(차별화/리스크/경쟁) — Opus 1회 + 과거 수주 제안서 RAG 컨텍스트

산출물 저장:
  storage/rfp_cache/<bid_id>.json   (RfpStructured.model_dump_json)
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from agents.base_agent import BaseAgent, ProgressCallback
from config.settings import (
    MODELS,
    RFP_CACHE_DIR,
    STORAGE_DIR,
    SYS,
    get_effective_company,
)
from schemas.models import AuditLogEntry, BidNotice, BidStatus
from schemas.rfp_schema import (
    RequirementCategory,
    RfpRequirement,
    RfpStructured,
    RfpTocChapter,
)
from tools import db
from tools.llm_client import chat
from tools.rfp_toc_extractor import extract_rfp_toc


_VALID_CATEGORIES = {c.value for c in RequirementCategory}


# ---------------------------------------------------------------------------
# 호출 2: 요구사항 + 평가기준 추출
# ---------------------------------------------------------------------------

REQ_SYSTEM = """당신은 한국 공공·민간 SI 제안서를 100건 이상 검토한 베테랑 PM입니다.
RFP 본문에서 모든 요구사항을 빠짐없이 추출하고, 평가기준 배점표를 식별합니다.
설명 없이 JSON 한 객체로만 응답합니다. 마크다운 코드블록 금지.

# 요구사항 분류 (정확히 이 8개 중 하나)
- 기능요구사항, 기술요구사항, 관리요구사항, 품질요구사항
- 보안요구사항, 인터페이스요구사항, 제약사항, 평가기준

# 출력 JSON 스키마
{
  "requirements": [
    {
      "req_id": "REQ-001",
      "category": "기능요구사항",
      "section": "RFP 원문 섹션 번호 또는 제목",
      "description": "요구사항 원문(1~2문장으로 압축 가능)",
      "mandatory": true,
      "keyword_tags": ["키워드1", "키워드2"]
    }
  ],
  "evaluation_criteria": [
    {"item": "기술능력", "max_score": 70, "details": "..."}
  ]
}

규칙:
- req_id는 REQ-001부터 순차 부여, 0 패딩 3자리.
- '...할 수 있어야 한다', '...를 제공한다', '...를 갖추어야 한다' 같은 표현은 모두 요구사항으로 본다.
- 추측·창작 금지. RFP에 명시된 내용만 추출.
- 평가기준은 '평가표', '배점표', '심사기준', '평가요소' 등의 표/섹션에서만 추출.
- 한국어로 출력."""


REQ_USER_TEMPLATE = """# 공고 정보
- 사업명: {title}
- 발주기관: {agency}
- 예산: {budget}원
- 사업기간: {duration}개월

# RFP 본문 (앞부분 발췌)
{rfp_text}"""


# ---------------------------------------------------------------------------
# 호출 3: 전략 메모 (RAG 컨텍스트 활용)
# ---------------------------------------------------------------------------

STRATEGY_SYSTEM = """당신은 한국 공공/민간 SI 제안서 win 전략가입니다.
RFP와 과거 수주 제안서 컨텍스트를 보고, 이 사업에서 우리가 강조해야 할 win theme을 정의합니다.
설명 없이 JSON 한 객체로만 응답합니다. 마크다운 코드블록 금지.

# 출력 JSON 스키마
{
  "differentiators": ["우리 회사만의 차별점 3~5개"],
  "win_themes": ["제안서 전반을 관통할 메시지 3개"],
  "risk_notes": ["발주처가 우려할 리스크 + 우리 대응방안"],
  "competitor_hints": ["예상 경쟁사 포지션과 우리 카운터"]
}

규칙:
- differentiators는 회사 보유 기술스택/팀 규모 안에서만 주장. 과대 주장 금지.
- win_themes는 RFP의 정책 배경/추진 목적과 직결되는 짧은 슬로건 형태.
- 과거 수주 제안서 컨텍스트가 제공되면 거기서 검증된 패턴을 우선 차용.
- 한국어로 출력."""


STRATEGY_USER_TEMPLATE = """# 회사 정보
- 회사명: {company_name}
- 보유 기술스택: {tech_stack}
- 팀 규모: {team_size}명

# 공고 정보
- 사업명: {title}
- 발주기관: {agency}
- 예산: {budget}원

# RFP 본문 (발췌)
{rfp_text}

# 과거 수주 제안서 컨텍스트 (RAG 검색 결과)
{rag_context}"""


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------

def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"JSON 추출 실패: {text[:200]}")
    return json.loads(m.group(0))


def _coerce_category(raw: str) -> RequirementCategory:
    """LLM 출력이 표준 카테고리에 없을 경우 키워드로 추정."""
    if raw in _VALID_CATEGORIES:
        return RequirementCategory(raw)
    text = (raw or "").lower()
    if any(k in text for k in ("기술", "tech")):
        return RequirementCategory.TECHNICAL
    if any(k in text for k in ("보안", "security")):
        return RequirementCategory.SECURITY
    if any(k in text for k in ("품질", "quality")):
        return RequirementCategory.QUALITY
    if any(k in text for k in ("관리", "관리요건", "운영")):
        return RequirementCategory.MANAGEMENT
    if any(k in text for k in ("인터페이스", "연계", "interface")):
        return RequirementCategory.INTERFACE
    if any(k in text for k in ("제약", "constraint")):
        return RequirementCategory.CONSTRAINT
    if any(k in text for k in ("평가", "심사", "evaluation")):
        return RequirementCategory.EVALUATION
    return RequirementCategory.FUNCTIONAL


def _toc_dicts_to_pydantic(chapters: List[dict]) -> List[RfpTocChapter]:
    """rfp_toc_extractor 출력 dict 트리 → RfpTocChapter Pydantic 트리."""
    out: List[RfpTocChapter] = []
    for i, ch in enumerate(chapters, start=1):
        parent_specialty = (ch.get("specialty") or "").strip() or None
        sub_models: List[RfpTocChapter] = []
        for j, sc in enumerate(ch.get("sub_chapters", []) or [], start=1):
            sub_models.append(RfpTocChapter(
                chapter_no=f"{i}.{j}",
                title=sc.get("title", ""),
                required_pages=sc.get("target_pages"),
                specialty=parent_specialty,  # sub는 parent의 specialty 상속
            ))
        out.append(RfpTocChapter(
            chapter_no=str(i),
            title=ch.get("title", ""),
            sub_chapters=sub_models,
            required_pages=ch.get("target_pages"),
            specialty=parent_specialty,
        ))
    return out


def _save_to_cache(rfp: RfpStructured) -> Path:
    """RfpStructured → storage/rfp_cache/<bid_id>.json"""
    path = RFP_CACHE_DIR / f"{rfp.bid_id}.json"
    path.write_text(rfp.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_strategy(bid_id: str) -> Optional[RfpStructured]:
    """저장된 전략 산출물 로드. WriterAgent / ReviewerAgent용."""
    path = RFP_CACHE_DIR / f"{bid_id}.json"
    if not path.exists():
        return None
    return RfpStructured.model_validate_json(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 에이전트
# ---------------------------------------------------------------------------

class StrategyAgent(BaseAgent):
    agent_name = "strategy"

    def __init__(self, progress_cb: Optional[ProgressCallback] = None) -> None:
        super().__init__(progress_cb)
        self.model = MODELS.strategy

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(min=1, max=8),
        retry=retry_if_exception_type(ValueError),
    )
    def run(self, bid: BidNotice) -> RfpStructured:
        rfp_text = (bid.rfp_full_text or bid.rfp_summary or "").strip()
        if not rfp_text:
            raise ValueError(
                f"[{self.agent_name}] {bid.bid_id}: RFP 본문이 비어 있어 전략 수립 불가"
            )

        self.progress(f"전략 수립 시작: {bid.bid_id} (model={self.model})")

        # 1) TOC 추출 — 기존 도구 재사용 (자체 LLM 호출, MODELS.rfp_struct)
        toc_dicts, global_ctx = extract_rfp_toc(bid)
        toc_models = _toc_dicts_to_pydantic(toc_dicts)
        target_pages = sum(
            c.get("target_pages", 0) for c in toc_dicts
        ) or SYS.target_pages_min
        self.progress(f"TOC 추출 완료: {len(toc_models)}챕터, 목표 {target_pages}p")

        # 2) 요구사항 + 평가기준
        requirements, eval_criteria = self._extract_requirements(bid, rfp_text)
        self.progress(
            f"요구사항 {len(requirements)}건 / 평가기준 {len(eval_criteria)}건 추출"
        )

        # 3) 전략 메모 (RAG 활용)
        strategy_notes = self._derive_strategy(bid, rfp_text, global_ctx)
        self.progress("전략 메모 도출 완료")

        # 글로벌 컨텍스트의 차별화 힌트와 LLM 산출 differentiators 병합 (중복 제거)
        differentiators = list(dict.fromkeys(
            list(global_ctx.get("differentiators", []))
            + strategy_notes.get("differentiators", [])
        ))[:8]

        rfp = RfpStructured(
            bid_id=bid.bid_id,
            title=bid.title,
            agency=bid.agency,
            requested_toc=toc_models,
            requirements=requirements,
            evaluation_criteria=eval_criteria,
            differentiators=differentiators,
            win_themes=strategy_notes.get("win_themes", []),
            risk_notes=strategy_notes.get("risk_notes", []),
            competitor_hints=strategy_notes.get("competitor_hints", []),
            target_pages=max(SYS.target_pages_min, min(target_pages, SYS.target_pages_max)),
        )

        cache_path = _save_to_cache(rfp)
        db.set_bid_status(bid.bid_id, BidStatus.STRATEGY_DONE)
        db.log_audit(AuditLogEntry(
            agent_name=self.agent_name,
            model=self.model,
            bid_id=bid.bid_id,
            note=(
                f"toc={len(toc_models)} req={len(requirements)} "
                f"eval={len(eval_criteria)} → {cache_path.name}"
            ),
        ))
        self.progress(f"전략 산출물 저장: {cache_path.name}")
        return rfp

    # ------------------------------------------------------------------
    # 내부 단계
    # ------------------------------------------------------------------

    def _extract_requirements(
        self, bid: BidNotice, rfp_text: str
    ) -> tuple[List[RfpRequirement], List[dict]]:
        user = REQ_USER_TEMPLATE.format(
            title=bid.title,
            agency=bid.agency,
            budget=f"{(bid.budget_krw or 0):,}",
            duration=bid.duration_months or "미상",
            rfp_text=rfp_text[:18000],
        )

        # 자체 retry: max_tokens를 1.5배씩 escalate, raw 저장. 3회 모두 실패 시 RuntimeError.
        # 외부 @retry는 ValueError만 잡으므로 RuntimeError는 즉시 종료(무의미한 재시도 차단).
        max_tokens = 16000  # A안: 8000 → 16000 (smoke_001처럼 큰 RFP에서 잘림 방지)
        max_tokens_cap = 32000
        last_err: Optional[Exception] = None
        raw = ""
        data: Optional[dict] = None
        for attempt in range(1, 4):
            try:
                raw = chat(
                    self.model,
                    REQ_SYSTEM,
                    user,
                    max_tokens=max_tokens,
                    cache_system=True,
                )
                data = _parse_json(raw)
                break
            except (ValueError, json.JSONDecodeError) as e:
                last_err = e
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                fail_dir = STORAGE_DIR / "logs"
                fail_dir.mkdir(parents=True, exist_ok=True)
                fail_path = fail_dir / (
                    f"strategize_failed_{bid.bid_id}_{ts}_attempt{attempt}.txt"
                )
                try:
                    fail_path.write_text(
                        f"# attempt={attempt}, max_tokens={max_tokens}, err={e}\n\n{raw}",
                        encoding="utf-8",
                    )
                except Exception:
                    pass
                logger.warning(
                    f"[{self.agent_name}] _extract_requirements attempt {attempt}/3 실패 "
                    f"(max_tokens={max_tokens}, err={e}); raw → {fail_path.name}"
                )
                max_tokens = min(int(max_tokens * 1.5), max_tokens_cap)
        if data is None:
            raise RuntimeError(
                f"[{self.agent_name}] _extract_requirements 3회 모두 실패. "
                f"마지막 에러: {last_err}. "
                f"raw output들이 storage/logs/strategize_failed_{bid.bid_id}_*.txt에 저장됨."
            )

        reqs_raw = data.get("requirements") or []
        requirements: List[RfpRequirement] = []
        for i, r in enumerate(reqs_raw, start=1):
            if not isinstance(r, dict):
                continue
            description = (r.get("description") or "").strip()
            if not description:
                continue
            req_id = (r.get("req_id") or "").strip() or f"REQ-{i:03d}"
            section = (r.get("section") or "").strip() or "(미상)"
            tags = [
                str(t).strip()
                for t in (r.get("keyword_tags") or [])
                if str(t).strip()
            ][:8]
            requirements.append(RfpRequirement(
                req_id=req_id,
                category=_coerce_category(r.get("category", "")),
                section=section,
                description=description,
                mandatory=bool(r.get("mandatory", True)),
                keyword_tags=tags,
            ))

        eval_raw = data.get("evaluation_criteria") or []
        eval_criteria = [c for c in eval_raw if isinstance(c, dict)]
        return requirements, eval_criteria

    def _derive_strategy(
        self, bid: BidNotice, rfp_text: str, global_ctx: dict
    ) -> dict:
        company = get_effective_company()
        rag_context = self._fetch_rag_context(bid)

        user = STRATEGY_USER_TEMPLATE.format(
            company_name=company.name,
            tech_stack=", ".join(company.tech_stack),
            team_size=company.team_size,
            title=bid.title,
            agency=bid.agency,
            budget=f"{(bid.budget_krw or 0):,}",
            rfp_text=rfp_text[:8000],
            rag_context=rag_context or "(과거 수주 제안서 데이터 없음)",
        )
        raw = chat(
            self.model,
            STRATEGY_SYSTEM,
            user,
            max_tokens=3000,
            cache_system=True,
        )
        try:
            data = _parse_json(raw)
        except Exception as e:
            logger.warning(
                f"[{self.agent_name}] 전략 메모 JSON 파싱 실패 ({bid.bid_id}): {e}"
            )
            return {}

        def _str_list(key: str, limit: int) -> List[str]:
            return [
                str(x).strip() for x in (data.get(key) or [])
                if str(x).strip()
            ][:limit]

        return {
            "differentiators": _str_list("differentiators", 5),
            "win_themes": _str_list("win_themes", 3),
            "risk_notes": _str_list("risk_notes", 8),
            "competitor_hints": _str_list("competitor_hints", 5),
        }

    def _fetch_rag_context(self, bid: BidNotice) -> str:
        """과거 수주 제안서 컨텍스트 검색. 의존성/데이터 누락 시 빈 문자열."""
        try:
            from rag.retrieve import format_context, retrieve
        except ImportError as e:
            logger.warning(f"[{self.agent_name}] RAG 모듈 없음 — 컨텍스트 생략: {e}")
            return ""
        try:
            query = f"{bid.title} {bid.agency} 차별화 win theme"
            results = retrieve(query, n_results=5, doc_type="pdf")
            return format_context(results, max_chars=4000)
        except Exception as e:
            logger.warning(
                f"[{self.agent_name}] RAG 검색 실패 ({bid.bid_id}): {e}"
            )
            return ""
