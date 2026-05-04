"""
WriterAgent — 제안서 본문 작성.

v1 ParkTeam(953줄)을 6-에이전트 체계에 맞춰 재구조화. 변경점:
  - 8명 specialist 카탈로그를 config/specialists.yaml에서 동적 로드 (외부화)
  - TOC 기획 책임은 StrategyAgent로 이관 → 여기서는 load_strategy()로 RfpStructured 소비
  - 수용표 매핑 강제: 각 섹션 작성 시 req_ids_covered 필수 기록
  - 스토리보드/슬라이드 렌더는 GraphicsAgent로 이관 → WriterAgent는 ProposalDraft만 산출

산출물:
  ProposalDraft (sections + req_ids_covered + slides_json) → db.save_proposal
  DOCX 본문 (storage/outputs/<bid_id>_proposal_v<N>.docx) → draft.docx_path
  BidStatus → DRAFT_DONE
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml
from loguru import logger

from agents.base_agent import BaseAgent, ProgressCallback
from agents.strategy_agent import load_strategy
from config.settings import (
    MODELS,
    ROOT_DIR,
    SYS,
    get_effective_company,
    get_extra_instructions,
)
from schemas.models import (
    AuditLogEntry,
    BidNotice,
    BidStatus,
    ProposalDraft,
    ProposalSection,
)
from schemas.rfp_schema import RfpRequirement, RfpStructured, RfpTocChapter
from tools import db
from tools.docx_generator import render_proposal_docx
from tools.llm_client import chat


SPECIALISTS_YAML = ROOT_DIR / "config" / "specialists.yaml"

# LLM 응답 후처리용 마커 (본문 / slides_json / 수용 req_ids 분리)
SLIDES_MARKER = "===SLIDES_JSON==="
REQS_MARKER = "===REQ_COVERAGE==="


# ---------------------------------------------------------------------------
# 공유 프롬프트 블록 (전 specialist 공통)
# ---------------------------------------------------------------------------

_BASE_TONE = """
[필수 작성 규칙]
- 한국 공식 문서체(평어체, '~한다' / '~할 것이다')로 작성한다.
- 두괄식 — 핵심 결론 → 근거 → 세부 항목 순으로 전개한다.
- 단순 나열 금지. 각 항목마다 'Why(왜) → How(어떻게) → Effect(효과)' 3박자를 갖춘다.
- 표/그림은 [표] [그림] 텍스트 마커로 표기하되, 표는 가능한 한 마크다운 표 형식으로 그려라.
- 출력은 마크다운 코드블록(```) 없이 본문만 작성한다.
- 거짓 실적·자격·수치를 절대 만들어내지 않는다 (제공된 회사 자산 안에서만 인용).
- 추상적 미사여구 대신 구체 수치·기간·인원·고객사명을 우선 사용한다.
- 발주처가 RFP에서 직접 사용한 단어를 본문에 의도적으로 인용하여 평가위원이 매칭을 쉽게 하도록 한다.
"""

_VISUAL_INSTRUCTION = """
[추가 — 핵심 섹션 시각화 규칙]
이 섹션은 핵심 섹션이다. 반드시 다음을 포함한다:
1. As-Is(현행) → To-Be(개선 후) 비교 — SLIDES_JSON에 `as_is_to_be_compare` 레이아웃 1장 이상.
2. To-Be 시스템 구성도 — SLIDES_JSON에 `system_architecture` 레이아웃 1장 이상.
3. 우리 회사만의 차별화 포인트(다른 경쟁사가 흉내낼 수 없는 부분)를 별도 단락 [차별화 포인트]로 명시한다.
4. 가능한 곳마다 자사 솔루션·자산을 자연스럽게 인용한다 (있을 때).
"""

_MOCKUP_INSTRUCTION = """
[추가 — 화면 mockup 강제]
본 섹션은 발주처가 '실제로 어떻게 생긴 화면인지' 보고 싶어하는 영역이다.
SLIDES_JSON에 `screen_mockup` 또는 `screen_mockup_grid` 레이아웃을 합계 5장 이상 포함하라.
- PC 화면(device="pc")과 모바일 화면(device="mobile")을 섞어 사용.
- regions의 kind는 hero/panel/grid/list/table/chart 중 적절한 것을 선택해 화면 다양성을 보여줄 것.
- 각 mockup의 label은 "응시자 메인", "감독관 콘솔" 같이 사용자/역할 + 화면 목적이 보이도록.
"""

_SLIDE_SPEC_INSTRUCTION = f"""
[★ 출력 마지막 — 슬라이드 스펙 + 요구사항 매핑]
본문 마지막에 반드시 두 마커를 차례로 적고, 각각 JSON 출력.
1) {SLIDES_MARKER}  다음 줄: 슬라이드 스펙 JSON 배열 (4~10장으로 본문 분할)
2) {REQS_MARKER}    다음 줄: 본 섹션이 수용한 RFP 요구사항 ID 배열 (예: ["REQ-001","REQ-014"])

사용 가능한 SLIDES_JSON layout 종류:
- {{"layout":"section_divider","title":"...","subtitle":"..."}}
- {{"layout":"title_bullets","title":"...","bullets":["...","...","..."]}}
- {{"layout":"two_column_compare","title":"...","left":{{"label":"AS-IS","items":["..."]}},"right":{{"label":"TO-BE","items":["..."]}}}}
- {{"layout":"diagram_layered","title":"...","diagram":{{"layers":[{{"name":"...","items":["..."]}}]}}}}
- {{"layout":"metric_cards","title":"...","metrics":[{{"value":"...","label":"...","note":"..."}}]}}
- {{"layout":"table","title":"...","table_data":{{"headers":["..."],"rows":[["..."]]}}}}
- {{"layout":"as_is_to_be_compare","title":"...","as_is_items":["..."],"to_be_items":["..."],"arrow_label":"..."}}
- {{"layout":"system_architecture","title":"...","architecture":{{"layers":[{{"name":"...","items":["..."]}}],"external":["..."]}}}}
- {{"layout":"process_flow","title":"...","flow_steps":[{{"step":"1","title":"...","desc":"..."}}]}}
- {{"layout":"screen_mockup","title":"...","mockup":{{"device":"pc","header":"...","regions":[{{"label":"...","kind":"hero|panel|grid|list|table|chart"}}]}}}}
- {{"layout":"screen_mockup_grid","title":"...","mockups":[{{"label":"...","device":"pc|mobile","header":"...","regions":[{{"label":"...","kind":"..."}}]}}]}}

규칙:
- 본인 섹션 첫 슬라이드는 거의 항상 section_divider 1장.
- AS-IS/TO-BE는 `as_is_to_be_compare` 도형을 우선 사용.
- 시스템 구성·아키텍처는 `system_architecture`를 우선 사용.
- 단계·절차·로드맵은 `process_flow`를 우선 사용.
- regions의 label은 12자 내외 한글로 구체적으로.
- 거짓 수치 금지.
- speaker_notes 필드(선택, 1~2문장)에 발표 시 강조점 적기.
- 각 마커 다음 줄은 JSON만. 설명·코드블록 금지.
- {REQS_MARKER} 배열은 user 프롬프트의 '본 섹션 매핑 후보' 목록에서 실제로 다룬 ID만 선택. 후보에 없는 ID 임의 추가 금지.
"""


# ---------------------------------------------------------------------------
# Specialist 카탈로그 — YAML 동적 로드
# ---------------------------------------------------------------------------

@dataclass
class Specialist:
    key: str
    name: str
    avatar: str
    role: str
    default_pages: int
    is_critical: bool
    inject_mockup: bool
    human_written: bool
    asset_kinds: Tuple[str, ...]
    use_reference: bool
    persona: str
    must_do: str

    @classmethod
    def from_yaml_dict(cls, d: dict) -> "Specialist":
        return cls(
            key=d["key"],
            name=d.get("name", d["key"]),
            avatar=d.get("avatar", "🧑‍💼"),
            role=d.get("role", ""),
            default_pages=int(d.get("default_pages", 8)),
            is_critical=bool(d.get("is_critical", False)),
            inject_mockup=bool(d.get("inject_mockup", False)),
            human_written=bool(d.get("human_written", False)),
            asset_kinds=tuple(d.get("asset_kinds") or ()),
            use_reference=bool(d.get("use_reference", True)),
            persona=(d.get("persona") or "").strip(),
            must_do=(d.get("must_do") or "").strip(),
        )


def load_specialists(path: Path = SPECIALISTS_YAML) -> Dict[str, Specialist]:
    """YAML → {key: Specialist} 딕셔너리. 모듈 단위 캐싱은 호출자에서."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = raw.get("specialists") or []
    cat: Dict[str, Specialist] = {}
    for item in items:
        sp = Specialist.from_yaml_dict(item)
        cat[sp.key] = sp
    if not cat:
        raise ValueError(f"specialists.yaml에서 specialist 0건 — 경로: {path}")
    return cat


def assemble_system_prompt(sp: Specialist) -> str:
    """Specialist의 persona/must_do + 공통 블록을 합쳐 LLM system 프롬프트로 직렬화."""
    parts = [sp.persona, "", "이 섹션에서 반드시 다룰 것:", sp.must_do, _BASE_TONE]
    if sp.is_critical:
        parts.append(_VISUAL_INSTRUCTION)
    if sp.inject_mockup:
        parts.append(_MOCKUP_INSTRUCTION)
    parts.append(_SLIDE_SPEC_INSTRUCTION)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LLM 응답 파서
# ---------------------------------------------------------------------------

def _extract_first_json_array(text: str) -> Optional[str]:
    start = text.find("[")
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
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _parse_response(raw: str) -> Tuple[str, List[Dict], List[str]]:
    """LLM 원본 응답 → (body, slides_json, req_ids_covered)."""
    body = raw
    slides: List[Dict] = []
    req_ids: List[str] = []

    if SLIDES_MARKER in body:
        body, _, tail = body.partition(SLIDES_MARKER)
        if REQS_MARKER in tail:
            slides_part, _, reqs_part = tail.partition(REQS_MARKER)
        else:
            slides_part, reqs_part = tail, ""

        json_text = _extract_first_json_array(slides_part)
        if json_text:
            try:
                arr = json.loads(json_text)
                slides = [
                    s for s in (arr if isinstance(arr, list) else [])
                    if isinstance(s, dict) and s.get("layout") and s.get("title")
                ]
            except Exception as e:
                logger.warning(f"[writer] slides_json 파싱 실패: {e}")

        if reqs_part.strip():
            json_text = _extract_first_json_array(reqs_part)
            if json_text:
                try:
                    arr = json.loads(json_text)
                    req_ids = [str(x).strip() for x in arr if str(x).strip()]
                except Exception as e:
                    logger.warning(f"[writer] req_coverage 파싱 실패: {e}")

    return body.strip(), slides, req_ids


# ---------------------------------------------------------------------------
# 컨텍스트 빌더
# ---------------------------------------------------------------------------

_ASSET_LABEL = {
    "solution": "💡 자사 솔루션",
    "case": "🏆 유사 수행실적",
    "cert": "🎖️ 보유 인증·자격",
    "metric": "📊 정량 자산 수치",
}


def _format_assets(kind: str, items: list[dict]) -> str:
    if not items:
        return ""
    lines = [f"## {_ASSET_LABEL.get(kind, kind)}"]
    for it in items[:8]:
        title = it.get("title", "")
        body = (it.get("body") or "")[:400]
        extra = it.get("extra") or {}
        meta = " / ".join(f"{k}:{v}" for k, v in extra.items() if v)
        head = f"- **{title}**" + (f"  ({meta})" if meta else "")
        lines.append(head)
        if body:
            lines.append(f"  {body}")
    return "\n".join(lines)


def _build_asset_block(specialist: Specialist) -> str:
    """specialist.asset_kinds → 회사 자산 텍스트 블록."""
    parts: List[str] = []
    for kind in specialist.asset_kinds:
        try:
            items = db.list_company_assets(kind=kind)
        except Exception as e:
            logger.debug(f"[writer] list_company_assets({kind}) 실패: {e}")
            continue
        block = _format_assets(kind, items)
        if block:
            parts.append(block)
    return "\n\n".join(parts)


def _filter_relevant_requirements(
    requirements: List[RfpRequirement], specialist: Specialist
) -> List[RfpRequirement]:
    """Specialist 카테고리·키워드와 매칭되는 RFP 요구사항만 후보로 추림.
    매칭 실패 시 전체 요구사항 반환 (LLM이 직접 판단하도록)."""
    if not requirements:
        return []

    # specialty → category 휴리스틱
    category_map = {
        "solution": ("기능요구사항", "기술요구사항", "인터페이스요구사항"),
        "methodology": ("관리요구사항",),
        "schedule": ("관리요구사항", "제약사항"),
        "quality": ("품질요구사항", "보안요구사항"),
        "company": ("평가기준",),
        "organization": ("관리요구사항",),
        "cost": ("제약사항",),
        "business": (),
    }
    target_cats = set(category_map.get(specialist.key, ()))
    if not target_cats:
        return requirements  # business 등은 전체 후보

    matched = [r for r in requirements if r.category.value in target_cats]
    return matched or requirements  # 매칭 0이면 전체 fallback


def _format_requirements(reqs: List[RfpRequirement]) -> str:
    if not reqs:
        return "(매핑 후보 없음)"
    lines = [f"- {r.req_id} [{r.category.value}] {r.description[:120]}" for r in reqs[:30]]
    return "\n".join(lines)


def _format_strategy_context(rfp: Optional[RfpStructured]) -> str:
    """StrategyAgent 산출물에서 win_themes/differentiators 등 짧은 컨텍스트 추출."""
    if not rfp:
        return ""
    lines = ["[★ 전체 제안 스토리 컨텍스트 — 본 섹션도 여기에 정렬]"]
    if rfp.win_themes:
        lines.append("- Win themes: " + " / ".join(rfp.win_themes[:3]))
    if rfp.differentiators:
        lines.append("- 자사 차별점:")
        for d in rfp.differentiators[:5]:
            lines.append(f"  · {d}")
    if rfp.risk_notes:
        lines.append("- 주요 리스크 메모:")
        for r in rfp.risk_notes[:3]:
            lines.append(f"  · {r}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Specialist 매핑
# ---------------------------------------------------------------------------

_TITLE_KEYWORD_HINTS = [
    (("기술", "솔루션", "구축내용", "제안내용", "시스템", "아키텍처", "기능"), "solution"),
    (("일정", "WBS", "스케줄", "마일"), "schedule"),
    (("방법론", "절차", "프로세스", "수행 방안"), "methodology"),
    (("품질", "보안", "유지보수", "운영"), "quality"),
    (("조직", "인력", "PM", "투입"), "organization"),
    (("회사", "실적", "회사소개", "수행실적"), "company"),
    (("비용", "산정", "가격", "효과", "투자"), "cost"),
]


def _resolve_specialty(chapter: RfpTocChapter, catalog: Dict[str, Specialist]) -> str:
    """Chapter → specialty key 결정. 명시값 우선, 없으면 제목 키워드 추정."""
    explicit = (chapter.specialty or "").strip()
    if explicit and explicit in catalog:
        return explicit
    title = chapter.title
    for keywords, key in _TITLE_KEYWORD_HINTS:
        if any(k in title for k in keywords):
            if key in catalog:
                return key
    return "business"  # 최종 fallback


# ---------------------------------------------------------------------------
# 에이전트
# ---------------------------------------------------------------------------

class WriterAgent(BaseAgent):
    agent_name = "writer"

    MAX_SUB_CALLS_PER_BID = 40  # 단일 공고에서 LLM 호출 폭증 방지
    PARALLEL_WORKERS = 3

    def __init__(self, progress_cb: Optional[ProgressCallback] = None) -> None:
        super().__init__(progress_cb)
        self.model = MODELS.writer
        self._catalog: Optional[Dict[str, Specialist]] = None

    @property
    def catalog(self) -> Dict[str, Specialist]:
        if self._catalog is None:
            self._catalog = load_specialists()
        return self._catalog

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    def run(self, bid: BidNotice, version: int = 1) -> ProposalDraft:
        rfp = load_strategy(bid.bid_id)
        if rfp is None or not rfp.requested_toc:
            raise ValueError(
                f"[{self.agent_name}] {bid.bid_id}: StrategyAgent 산출물(RfpStructured) 없음 — "
                "선행으로 StrategyAgent.run(bid)을 호출하라."
            )

        toc = self._coalesce_subs(list(rfp.requested_toc))
        total = len(toc)
        self.progress(
            f"작성 시작: {bid.bid_id} v{version} ({total}챕터, model={self.model})"
        )

        sections = self._write_all_chapters(bid, rfp, toc, version)
        sections.sort(key=lambda x: x.order)

        target_pages = max(SYS.target_pages_min, min(rfp.target_pages, SYS.target_pages_max))
        draft = ProposalDraft(
            bid_id=bid.bid_id,
            version=version,
            target_pages=target_pages,
            sections=sections,
        )

        # DOCX 본문 렌더 (LLM 호출 없는 순수 렌더링).
        # 실패 시 경고만 — PPTX/검수 등 후속 단계는 ProposalDraft 본문(DB)으로 진행 가능.
        try:
            docx_path = render_proposal_docx(draft, bid.title)
            draft.docx_path = str(docx_path)
            self.progress(f"DOCX 본문 생성: {docx_path.name}")
        except Exception as e:
            logger.warning(
                f"[{self.agent_name}] DOCX 렌더 실패 (무시, 계속 진행): {e}"
            )

        db.save_proposal(draft)
        db.set_bid_status(bid.bid_id, BidStatus.DRAFT_DONE)
        self.progress(f"작성 완료: {len(sections)}섹션, 목표 {target_pages}p")
        return draft

    # ------------------------------------------------------------------
    # 내부 단계
    # ------------------------------------------------------------------

    def _coalesce_subs(self, toc: List[RfpTocChapter]) -> List[RfpTocChapter]:
        """sub_chapters 합계가 한도를 넘으면 큰 챕터의 sub를 3개 묶음으로 병합."""
        total_sub = sum(len(c.sub_chapters) for c in toc)
        if total_sub <= self.MAX_SUB_CALLS_PER_BID:
            return toc

        logger.warning(
            f"[{self.agent_name}] sub-call 합계 {total_sub} > "
            f"{self.MAX_SUB_CALLS_PER_BID} — 큰 챕터의 sub 병합"
        )
        out: List[RfpTocChapter] = []
        for ch in toc:
            subs = ch.sub_chapters
            if len(subs) <= 3:
                out.append(ch)
                continue
            chunk = (len(subs) + 2) // 3
            merged: List[RfpTocChapter] = []
            for i in range(0, len(subs), chunk):
                group = subs[i:i + chunk]
                merged.append(RfpTocChapter(
                    chapter_no=group[0].chapter_no,
                    title=group[0].title + (
                        f" 외 {len(group) - 1}개" if len(group) > 1 else ""
                    ),
                    required_pages=sum(g.required_pages or 1 for g in group),
                    specialty=group[0].specialty,
                ))
            out.append(RfpTocChapter(
                chapter_no=ch.chapter_no,
                title=ch.title,
                sub_chapters=merged,
                required_pages=ch.required_pages,
                specialty=ch.specialty,
                evaluation_weight=ch.evaluation_weight,
            ))
        new_total = sum(len(c.sub_chapters) for c in out)
        logger.info(f"[{self.agent_name}] sub-call 병합 후 합계: {new_total}")
        return out

    def _write_all_chapters(
        self,
        bid: BidNotice,
        rfp: RfpStructured,
        toc: List[RfpTocChapter],
        version: int,
    ) -> List[ProposalSection]:
        sections: List[ProposalSection] = []
        with ThreadPoolExecutor(max_workers=self.PARALLEL_WORKERS) as ex:
            futures = {
                ex.submit(
                    self._write_chapter, bid, rfp, ch, idx, version
                ): (idx, ch)
                for idx, ch in enumerate(toc)
            }
            for fut in as_completed(futures):
                idx, ch = futures[fut]
                try:
                    sections.append(fut.result())
                    self.progress(f"섹션 완료: {ch.title}")
                except Exception as e:
                    logger.error(
                        f"[{self.agent_name}] 섹션 실패 ({ch.title}): {e}"
                    )
                    sections.append(ProposalSection(
                        title=ch.title,
                        body=f"(섹션 생성 실패: {e})",
                        order=idx,
                    ))
        return sections

    def _write_chapter(
        self,
        bid: BidNotice,
        rfp: RfpStructured,
        chapter: RfpTocChapter,
        order: int,
        version: int,
    ) -> ProposalSection:
        specialty = _resolve_specialty(chapter, self.catalog)
        sp = self.catalog[specialty]

        # 사람이 직접 채우는 영역은 placeholder만
        if sp.human_written:
            return self._make_placeholder(chapter, sp, order)

        # 작성 후보 요구사항
        candidates = _filter_relevant_requirements(rfp.requirements, sp)

        # sub_chapters → 각각 LLM 1회. 없으면 단일 호출.
        if chapter.sub_chapters:
            return self._write_with_subs(
                bid, rfp, chapter, sp, candidates, order, version
            )

        body, slides, req_ids = self._call_llm(
            bid=bid, rfp=rfp, sp=sp,
            section_title=chapter.title,
            section_brief="",
            target_pages=chapter.required_pages or sp.default_pages,
            candidates=candidates,
            version=version,
            is_subchapter=False,
            parent_title="",
        )
        if not slides:
            slides = [{
                "layout": "section_divider",
                "title": chapter.title,
                "subtitle": sp.role,
            }]
        return ProposalSection(
            title=chapter.title,
            body=body,
            order=order,
            specialty=sp.key,
            owner_name=sp.name,
            req_ids_covered=req_ids,
            slides_json=slides,
        )

    def _write_with_subs(
        self,
        bid: BidNotice,
        rfp: RfpStructured,
        chapter: RfpTocChapter,
        sp: Specialist,
        candidates: List[RfpRequirement],
        order: int,
        version: int,
    ) -> ProposalSection:
        body_parts: List[str] = [f"# {chapter.title}\n"]
        slides: List[Dict] = [{
            "layout": "section_divider",
            "title": chapter.title,
            "subtitle": sp.role,
        }]
        all_req_ids: List[str] = []

        for sub in chapter.sub_chapters:
            try:
                sub_body, sub_slides, sub_req_ids = self._call_llm(
                    bid=bid, rfp=rfp, sp=sp,
                    section_title=sub.title,
                    section_brief="",
                    target_pages=sub.required_pages or 2,
                    candidates=candidates,
                    version=version,
                    is_subchapter=True,
                    parent_title=chapter.title,
                )
            except Exception as e:
                logger.error(
                    f"[{self.agent_name}/{sp.name}] sub-chapter 실패 ({sub.title}): {e}"
                )
                sub_body = f"\n## {sub.title}\n\n(sub-chapter 생성 실패: {e})\n"
                sub_slides, sub_req_ids = [], []
            body_parts.append(f"\n## {sub.title}\n\n{sub_body}\n")
            for sl in sub_slides:
                if sl.get("layout") == "section_divider":
                    continue  # 상위 1회만
                slides.append(sl)
            all_req_ids.extend(sub_req_ids)

        # 중복 제거하면서 입력 순서 유지
        unique_req_ids = list(dict.fromkeys(all_req_ids))

        return ProposalSection(
            title=chapter.title,
            body="\n".join(body_parts),
            order=order,
            specialty=sp.key,
            owner_name=sp.name,
            req_ids_covered=unique_req_ids,
            slides_json=slides,
        )

    def _call_llm(
        self,
        *,
        bid: BidNotice,
        rfp: RfpStructured,
        sp: Specialist,
        section_title: str,
        section_brief: str,
        target_pages: int,
        candidates: List[RfpRequirement],
        version: int,
        is_subchapter: bool,
        parent_title: str,
    ) -> Tuple[str, List[Dict], List[str]]:
        company = get_effective_company()
        team_extra = get_extra_instructions("writer")
        company_intro = getattr(company, "intro_text", "") or ""
        differentiators = getattr(company, "differentiators", "") or ""

        target_chars = target_pages * 600
        # 16K cap: 본문 + SLIDES_JSON + REQ_COVERAGE 마커 모두 한 응답에 들어가야 함.
        # 8K 시점엔 본문이 cap을 채우고 마지막 마커가 잘려 req_ids_covered 누락 발생.
        max_tokens = min(16000, max(1500, int(target_chars * 2.5)))
        max_tokens_with_meta = min(16000, max_tokens + 1500)

        sub_note = ""
        if is_subchapter and parent_title:
            sub_note = (
                f"\n# 본 호출은 상위 챕터 '{parent_title}'의 sub-챕터 작성이다.\n"
                "- 상위 챕터 흐름 안에서 본인 sub-챕터만 책임지고 작성한다.\n"
                "- section_divider 슬라이드는 만들지 말고 본문 슬라이드부터 시작한다 (상위 간지는 따로 들어간다).\n"
            )

        version_note = (
            f"# 개정 안내\n버전 v{version} (수정 지시 반영)" if version > 1 else ""
        )

        asset_block = _build_asset_block(sp)
        strategy_block = _format_strategy_context(rfp)
        req_block = _format_requirements(candidates)
        rfp_text = (bid.rfp_full_text or bid.rfp_summary or "")[:18000]

        user_prompt = f"""# 작성 섹션
- 제목: {section_title}
- 담당 전문가: {sp.avatar} {sp.name} ({sp.role})
- 목표 분량: 약 {target_pages}페이지 ({target_chars}자 내외)
- 추가 작성 지시: {section_brief or '(없음)'}
{sub_note}
# 본 섹션 매핑 후보 (수용표 강제 입력)
다음 RFP 요구사항 중 본 섹션이 다룬 ID만 {REQS_MARKER} 배열에 적어라. 후보에 없는 ID 임의 추가 금지.
{req_block}

# 제안 회사 (실제 정보 — 이 안에서만 인용)
- 회사명: {company.name}
- 대표: {company.ceo}
- 사업자번호: {company.biz_num}
- 보유 기술: {', '.join(company.tech_stack)}
- 팀 규모: {company.team_size}명
- 회사 소개(원문):
{company_intro[:3000] if company_intro else '(미등록)'}
- 우리 회사 차별점:
{differentiators[:1500] if differentiators else '(미등록)'}

# 공고 정보
- 사업명: {bid.title}
- 발주기관: {bid.agency}
- 예산: {(bid.budget_krw or 0):,}원
- 사업기간: {bid.duration_months or '미상'}개월
- RFP 본문:
{rfp_text}

# 본 섹션을 위해 큐레이션된 회사 자산
{asset_block or '(이 섹션에 매칭되는 자산이 등록되어 있지 않다. 회사 정보·차별점 위주로 작성하라.)'}

{strategy_block}

# 팀장 추가 지시
{team_extra or '(없음)'}

{version_note}

위 정보로 본인의 전문 영역에 충실한 본문을 작성하라.
시스템 프롬프트의 형식 규칙·필수 항목·분량 목표를 모두 지킬 것.
"""

        system = assemble_system_prompt(sp)
        raw = chat(
            self.model,
            system,
            user_prompt,
            max_tokens=max_tokens_with_meta,
            cache_system=True,  # 같은 specialist 연속 호출 시 캐시 히트
        )
        body, slides, req_ids = _parse_response(raw)

        # 후보에 없는 req_id가 끼어들면 제거
        valid_ids = {r.req_id for r in candidates}
        req_ids = [rid for rid in req_ids if rid in valid_ids]

        db.log_audit(AuditLogEntry(
            agent_name=f"{self.agent_name}/{sp.name}",
            model=self.model,
            bid_id=bid.bid_id,
            note=(
                f"section={section_title} pages={target_pages} "
                f"slides={len(slides)} reqs={len(req_ids)} v={version}"
            ),
        ))
        return body, slides, req_ids

    def _make_placeholder(
        self, chapter: RfpTocChapter, sp: Specialist, order: int
    ) -> ProposalSection:
        body = (
            f"# {chapter.title} (사람 작성 예정)\n\n"
            "이 섹션은 사장님(또는 영업/HR 담당)이 직접 작성합니다. "
            "회사 소개·유사 실적·투입 인력의 핵심 정보(인물명, 자격증, 고객사명, 금액)는 "
            "AI가 임의로 만들 수 없으므로 빈 자리만 잡아 둡니다.\n\n"
            "📌 PPT 빌드 시 표지·간지만 삽입되고, 본문은 빈 페이지 1~2장으로 생성됩니다.\n"
        )
        slides = [
            {"layout": "section_divider", "title": chapter.title, "subtitle": "사장님 직접 작성 영역"},
            {"layout": "blank_placeholder", "title": chapter.title,
             "placeholder_reason": "회사 소개·실적·인력 정보는 영업/HR 담당이 직접 채웁니다."},
            {"layout": "blank_placeholder", "title": f"{chapter.title} (보충 페이지)",
             "placeholder_reason": "필요 시 1장 더 사용하세요."},
        ]
        return ProposalSection(
            title=chapter.title,
            body=body,
            order=order,
            specialty=sp.key,
            owner_name=sp.name,
            slides_json=slides,
        )
