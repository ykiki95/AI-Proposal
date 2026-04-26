"""
SQLite 기반 영속성 계층.
SQLAlchemy ORM으로 동시 접근 안전성을 확보한다.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, List, Optional

from loguru import logger
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config.settings import DB_PATH
from schemas.models import (
    AuditLogEntry,
    BidEvaluation,
    BidNotice,
    BidStatus,
    ProposalDraft,
    QualityReport,
)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# ORM 모델
# ---------------------------------------------------------------------------

class BidRow(Base):
    __tablename__ = "bids"
    bid_id = Column(String, primary_key=True)
    source = Column(String)
    agency = Column(String)
    title = Column(String)
    budget_krw = Column(Integer, nullable=True)
    duration_months = Column(Integer, nullable=True)
    qualifications = Column(JSON, default=list)
    deadline = Column(DateTime)
    rfp_summary = Column(Text)
    rfp_url = Column(String)
    rfp_full_text = Column(Text, nullable=True)
    rfp_deep_analysis = Column(Text, nullable=True)  # 심층분석 캐시 (파견·소재지·독소조항 등)
    collected_at = Column(DateTime, default=datetime.now)
    status = Column(String, default=BidStatus.COLLECTED.value)
    is_pinned = Column(Integer, default=0)  # ⭐ 사장님 찜 (1차 점수와 무관하게 게이트1 자동 진입)


class EvaluationRow(Base):
    __tablename__ = "evaluations"
    bid_id = Column(String, primary_key=True)
    fit_score = Column(Integer)
    score_breakdown = Column(JSON, default=dict)
    recommendation = Column(String)
    rationale = Column(Text)
    risk_factors = Column(JSON, default=list)
    opportunity_factors = Column(JSON, default=list)
    evaluated_at = Column(DateTime, default=datetime.now)


class ProposalRow(Base):
    __tablename__ = "proposals"
    id = Column(Integer, primary_key=True, autoincrement=True)
    bid_id = Column(String, index=True)
    version = Column(Integer)
    sections = Column(JSON, default=list)
    docx_path = Column(String, nullable=True)
    pptx_path = Column(String, nullable=True)
    storyboard_path = Column(String, nullable=True)
    script_path = Column(String, nullable=True)  # 🎤 발표 시나리오(DOCX) 경로
    generated_at = Column(DateTime, default=datetime.now)


class QualityRow(Base):
    __tablename__ = "quality_reports"
    id = Column(Integer, primary_key=True, autoincrement=True)
    bid_id = Column(String, index=True)
    version = Column(Integer)
    spelling_issues = Column(JSON, default=list)
    consistency_issues = Column(JSON, default=list)
    rfp_coverage = Column(JSON, default=dict)
    overall_grade = Column(String)
    action_items = Column(JSON, default=list)
    reviewed_at = Column(DateTime, default=datetime.now)


class AuditRow(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_name = Column(String)
    model = Column(String)
    tokens_in = Column(Integer, default=0)
    tokens_out = Column(Integer, default=0)
    cost_usd = Column(Float, default=0.0)
    bid_id = Column(String, nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class AgentConfigRow(Base):
    """대시보드에서 변경 가능한 에이전트 런타임 설정."""
    __tablename__ = "agent_config"
    agent_key = Column(String, primary_key=True)  # kim/lee/park/choi/oh
    model = Column(String, nullable=True)
    extra_instructions = Column(Text, nullable=True)
    keywords = Column(JSON, default=list)  # 김탐정 수집 키워드 등
    updated_at = Column(DateTime, default=datetime.now)


class CompanyProfileRow(Base):
    """회사 정보 (싱글턴 row id=1)."""
    __tablename__ = "company_profile"
    id = Column(Integer, primary_key=True, default=1)
    name = Column(String)
    ceo = Column(String)
    biz_num = Column(String)
    tech_stack = Column(JSON, default=list)
    team_size = Column(Integer, default=15)
    typical_budget_min = Column(Integer, default=30_000_000)
    typical_budget_max = Column(Integer, default=500_000_000)
    intro_text = Column(Text, nullable=True)         # 회사소개서 본문(원문)
    brochure_path = Column(String, nullable=True)    # 업로드된 회사소개서 파일 경로
    differentiators = Column(Text, nullable=True)    # 우리 회사만의 차별점
    reference_proposal_text = Column(Text, nullable=True)   # 참고 제안서 본문 (붙여넣기)
    reference_proposal_path = Column(String, nullable=True) # 참고 제안서 첨부 파일 경로
    reference_instructions = Column(Text, nullable=True)    # 참고 지시 텍스트 (톤·문체·강조점 등)
    # 사장님이 직접 업로드하는 PPTX 마스터 슬롯 4개 (정디자가 선택해 사용)
    pptx_master_1_path = Column(String, nullable=True)
    pptx_master_1_label = Column(String, nullable=True)
    pptx_master_2_path = Column(String, nullable=True)
    pptx_master_2_label = Column(String, nullable=True)
    pptx_master_3_path = Column(String, nullable=True)
    pptx_master_3_label = Column(String, nullable=True)
    pptx_master_4_path = Column(String, nullable=True)
    pptx_master_4_label = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.now)


class ProposalTocRow(Base):
    """공고별 가변 목차 - 박제안 팀장이 사용. 사용자가 수정 가능."""
    __tablename__ = "proposal_toc"
    bid_id = Column(String, primary_key=True)
    sections = Column(JSON, default=list)
    # 각 섹션: {title, specialty, display_role, target_pages, brief, sub_chapters?:[{title,target_pages,brief}]}
    global_context = Column(JSON, default=dict, nullable=True)
    # {vision, key_keywords[], differentiators[], transitions[]}
    updated_at = Column(DateTime, default=datetime.now)


class ReferenceProposalRow(Base):
    """레퍼런스 제안서 라이브러리 (다건). 박제안 팀이 톤·구조·표현을 모방."""
    __tablename__ = "reference_proposals"
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String)              # 예: "경찰청 통합신고대응센터"
    client = Column(String)             # 발주처
    domain = Column(String)             # 도메인 태그 예: "공공/AI/SI"
    won = Column(Integer, default=1)    # 1=수주성공, 0=참고용
    body_text = Column(Text)            # 본문 텍스트(붙여넣기)
    file_path = Column(String, nullable=True)  # 첨부 파일 경로
    instructions = Column(Text, nullable=True)  # 모방 지시 (이 부분만 따라하라 등)
    created_at = Column(DateTime, default=datetime.now)


class CompanyAssetRow(Base):
    """회사 자산 카탈로그.
    kind: solution(자사 솔루션) / case(수행 실적) / cert(인증·자격) / metric(정량 수치)
    extra(JSON): 자유 메타 (year, client, value, unit 등)
    """
    __tablename__ = "company_assets"
    id = Column(Integer, primary_key=True, autoincrement=True)
    kind = Column(String, index=True)
    title = Column(String)
    body = Column(Text)
    extra = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.now)


class ActivityRow(Base):
    """에이전트 실시간 활동 로그 (대시보드 타임라인용)."""
    __tablename__ = "activity_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_key = Column(String, index=True)
    event = Column(String)         # start/finish/error/info
    bid_id = Column(String, nullable=True)
    message = Column(Text)
    created_at = Column(DateTime, default=datetime.now)


# ---------------------------------------------------------------------------
# 엔진
# ---------------------------------------------------------------------------

_engine = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=False,
    future=True,
    connect_args={"check_same_thread": False},
)
_SessionFactory = sessionmaker(_engine, expire_on_commit=False, class_=Session)


def _migrate_sqlite() -> None:
    """기존 DB에 없는 컬럼을 ALTER TABLE로 추가하는 경량 마이그레이션."""
    expected_columns = {
        "agent_config": [
            ("keywords", "JSON"),
        ],
        "company_profile": [
            ("reference_proposal_text", "TEXT"),
            ("reference_proposal_path", "VARCHAR"),
            ("reference_instructions", "TEXT"),
            ("pptx_master_1_path", "VARCHAR"),
            ("pptx_master_1_label", "VARCHAR"),
            ("pptx_master_2_path", "VARCHAR"),
            ("pptx_master_2_label", "VARCHAR"),
            ("pptx_master_3_path", "VARCHAR"),
            ("pptx_master_3_label", "VARCHAR"),
            ("pptx_master_4_path", "VARCHAR"),
            ("pptx_master_4_label", "VARCHAR"),
        ],
        "bids": [
            ("rfp_deep_analysis", "TEXT"),
            ("is_pinned", "INTEGER"),
        ],
        "proposals": [
            ("storyboard_path", "VARCHAR"),
            ("script_path", "VARCHAR"),
        ],
        "proposal_toc": [
            ("global_context", "JSON"),
        ],
        # 향후 컬럼 추가가 있을 때 여기에 누적
    }
    with _engine.begin() as conn:
        for table, cols in expected_columns.items():
            try:
                rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            except Exception:
                continue  # 테이블 자체가 없으면 create_all이 만들어줌
            existing = {r[1] for r in rows}
            for col_name, col_type in cols:
                if col_name not in existing:
                    try:
                        conn.exec_driver_sql(
                            f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"
                        )
                        logger.info(f"마이그레이션: {table}.{col_name} 컬럼 추가")
                    except Exception as e:
                        logger.warning(f"컬럼 추가 실패 {table}.{col_name}: {e}")


def init_db() -> None:
    Base.metadata.create_all(_engine)
    _migrate_sqlite()
    logger.info(f"DB 초기화 완료 → {DB_PATH}")


@contextmanager
def session_scope() -> Iterator[Session]:
    s = _SessionFactory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Repository 함수
# ---------------------------------------------------------------------------

def upsert_bid(bid: BidNotice) -> bool:
    """공고 upsert. 신규면 True 반환 (중복 제거 판정)."""
    with session_scope() as s:
        existing = s.get(BidRow, bid.bid_id)
        if existing:
            return False
        s.add(BidRow(
            bid_id=bid.bid_id,
            source=bid.source,
            agency=bid.agency,
            title=bid.title,
            budget_krw=bid.budget_krw,
            duration_months=bid.duration_months,
            qualifications=bid.qualifications,
            deadline=bid.deadline,
            rfp_summary=bid.rfp_summary,
            rfp_url=bid.rfp_url,
            rfp_full_text=bid.rfp_full_text,
            collected_at=bid.collected_at,
            status=BidStatus.COLLECTED.value,
        ))
        return True


def list_bids(status: Optional[BidStatus] = None) -> List[BidNotice]:
    with session_scope() as s:
        stmt = select(BidRow)
        if status:
            stmt = stmt.where(BidRow.status == status.value)
        rows = s.execute(stmt).scalars().all()
        return [_row_to_bid(r) for r in rows]


def get_bid(bid_id: str) -> Optional[BidNotice]:
    with session_scope() as s:
        r = s.get(BidRow, bid_id)
        return _row_to_bid(r) if r else None


def set_bid_status(bid_id: str, status: BidStatus) -> None:
    with session_scope() as s:
        r = s.get(BidRow, bid_id)
        if r:
            r.status = status.value


def save_evaluation(ev: BidEvaluation) -> None:
    with session_scope() as s:
        r = s.get(EvaluationRow, ev.bid_id)
        if r is None:
            r = EvaluationRow(bid_id=ev.bid_id)
            s.add(r)
        r.fit_score = ev.fit_score
        r.score_breakdown = ev.score_breakdown
        r.recommendation = ev.recommendation
        r.rationale = ev.rationale
        r.risk_factors = ev.risk_factors
        r.opportunity_factors = ev.opportunity_factors
        r.evaluated_at = ev.evaluated_at
        bid = s.get(BidRow, ev.bid_id)
        if bid:
            bid.status = BidStatus.EVALUATED.value


def get_evaluation(bid_id: str) -> Optional[BidEvaluation]:
    with session_scope() as s:
        r = s.get(EvaluationRow, bid_id)
        if not r:
            return None
        return BidEvaluation(
            bid_id=r.bid_id,
            fit_score=r.fit_score,
            score_breakdown=r.score_breakdown or {},
            recommendation=r.recommendation,
            rationale=r.rationale,
            risk_factors=r.risk_factors or [],
            opportunity_factors=r.opportunity_factors or [],
            evaluated_at=r.evaluated_at,
        )


def save_proposal(p: ProposalDraft) -> None:
    with session_scope() as s:
        s.add(ProposalRow(
            bid_id=p.bid_id,
            version=p.version,
            sections=[sec.model_dump() for sec in p.sections],
            docx_path=p.docx_path,
            pptx_path=p.pptx_path,
            generated_at=p.generated_at,
        ))


def set_bid_pinned(bid_id: str, pinned: bool) -> None:
    """⭐ 찜 토글. 찜한 공고는 1차 점수와 무관하게 게이트1로 자동 진입."""
    with session_scope() as s:
        r = s.get(BidRow, bid_id)
        if r:
            r.is_pinned = 1 if pinned else 0
            # 찜하면 즉시 게이트1로 보냄. 해제는 상태 그대로 유지(이미 진입했을 수 있음).
            if pinned and r.status == BidStatus.COLLECTED.value:
                r.status = BidStatus.AWAITING_APPROVAL.value


def is_bid_pinned(bid_id: str) -> bool:
    with session_scope() as s:
        r = s.get(BidRow, bid_id)
        return bool(r and r.is_pinned)


def update_proposal_script(bid_id: str, version: int, script_path: str) -> None:
    """발표문(DOCX) 경로를 ProposalRow에 반영."""
    from sqlalchemy import select
    with session_scope() as s:
        r = s.execute(
            select(ProposalRow)
            .where(ProposalRow.bid_id == bid_id, ProposalRow.version == version)
        ).scalar_one_or_none()
        if r:
            r.script_path = script_path


def update_proposal_storyboard(bid_id: str, version: int, storyboard_path: str) -> None:
    """박제안이 생성한 기획 PPT(스토리보드) 경로를 ProposalRow에 반영."""
    from sqlalchemy import select
    with session_scope() as s:
        r = s.execute(
            select(ProposalRow)
            .where(ProposalRow.bid_id == bid_id, ProposalRow.version == version)
        ).scalar_one_or_none()
        if r:
            r.storyboard_path = storyboard_path


def get_design_template() -> str:
    """대시보드에서 사용자가 고른 디자인 템플릿 키. 미설정 시 'navy'.
    저장 위치: AgentConfigRow(agent_key='pptx').model 필드를 재활용."""
    try:
        with session_scope() as s:
            r = s.get(AgentConfigRow, "pptx")
            return (r.model if r and r.model else "navy")
    except Exception:
        return "navy"


def set_design_template(template: str) -> None:
    """디자인 템플릿 선택값 저장. agent_config('pptx').model 슬롯에 저장."""
    with session_scope() as s:
        r = s.get(AgentConfigRow, "pptx")
        if r is None:
            r = AgentConfigRow(agent_key="pptx")
            s.add(r)
        r.model = template
        r.extra_instructions = ""
        r.updated_at = datetime.now()


def update_proposal_pptx(bid_id: str, version: int, pptx_path: str) -> None:
    """최피티가 생성한 PPTX 경로를 기존 ProposalRow에 반영."""
    with session_scope() as s:
        r = s.execute(
            select(ProposalRow)
            .where(ProposalRow.bid_id == bid_id, ProposalRow.version == version)
        ).scalar_one_or_none()
        if r:
            r.pptx_path = pptx_path


def update_bid_full_text(bid_id: str, full_text: str) -> None:
    """공고 RFP 원문 본문을 갱신. 사용자가 대시보드에서 직접 붙여넣을 때."""
    with session_scope() as s:
        r = s.get(BidRow, bid_id)
        if r:
            r.rfp_full_text = full_text
            # 본문이 들어왔으니 직전 심층분석 캐시는 무효화
            r.rfp_deep_analysis = None


def clear_demo_data() -> dict:
    """공고/평가/제안서/품질/활동/감사/목차 등 시연 데이터 일괄 삭제.
    회사 프로필·자산 라이브러리·레퍼런스 라이브러리·에이전트 설정은 보존."""
    counts = {}
    with session_scope() as s:
        for cls in (
            BidRow, EvaluationRow, ProposalRow, QualityRow,
            AuditRow, ActivityRow, ProposalTocRow,
        ):
            n = s.query(cls).delete()
            counts[cls.__tablename__] = n
    logger.info(f"시연 데이터 초기화: {counts}")
    return counts


def update_bid_deep_analysis(bid_id: str, analysis: str) -> None:
    with session_scope() as s:
        r = s.get(BidRow, bid_id)
        if r:
            r.rfp_deep_analysis = analysis


def save_quality_report(q: QualityReport) -> None:
    with session_scope() as s:
        s.add(QualityRow(
            bid_id=q.bid_id,
            version=q.version,
            spelling_issues=q.spelling_issues,
            consistency_issues=q.consistency_issues,
            rfp_coverage=q.rfp_coverage,
            overall_grade=q.overall_grade,
            action_items=q.action_items,
            reviewed_at=q.reviewed_at,
        ))


def get_agent_overrides() -> dict[str, dict]:
    """대시보드에서 저장한 에이전트별 모델/지시/키워드 오버라이드를 dict로 반환."""
    with session_scope() as s:
        rows = s.execute(select(AgentConfigRow)).scalars().all()
        return {
            r.agent_key: {
                "model": r.model,
                "extra_instructions": r.extra_instructions or "",
                "keywords": r.keywords or [],
            }
            for r in rows
        }


def save_agent_override(
    agent_key: str,
    model: Optional[str],
    instructions: str,
    keywords: Optional[list] = None,
) -> None:
    with session_scope() as s:
        r = s.get(AgentConfigRow, agent_key)
        if r is None:
            r = AgentConfigRow(agent_key=agent_key)
            s.add(r)
        r.model = model
        r.extra_instructions = instructions
        if keywords is not None:
            r.keywords = keywords
        r.updated_at = datetime.now()


def get_company_profile() -> Optional[dict]:
    with session_scope() as s:
        r = s.get(CompanyProfileRow, 1)
        if not r:
            return None
        return {
            "name": r.name,
            "ceo": r.ceo,
            "biz_num": r.biz_num,
            "tech_stack": r.tech_stack or [],
            "team_size": r.team_size,
            "typical_budget_min": r.typical_budget_min,
            "typical_budget_max": r.typical_budget_max,
            "intro_text": r.intro_text or "",
            "brochure_path": r.brochure_path,
            "differentiators": r.differentiators or "",
            "reference_proposal_text": r.reference_proposal_text or "",
            "reference_proposal_path": r.reference_proposal_path,
            "reference_instructions": r.reference_instructions or "",
            "pptx_master_1_path": r.pptx_master_1_path,
            "pptx_master_1_label": r.pptx_master_1_label or "",
            "pptx_master_2_path": r.pptx_master_2_path,
            "pptx_master_2_label": r.pptx_master_2_label or "",
            "pptx_master_3_path": r.pptx_master_3_path,
            "pptx_master_3_label": r.pptx_master_3_label or "",
            "pptx_master_4_path": r.pptx_master_4_path,
            "pptx_master_4_label": r.pptx_master_4_label or "",
            "updated_at": r.updated_at,
        }


def save_company_profile(data: dict) -> None:
    with session_scope() as s:
        r = s.get(CompanyProfileRow, 1)
        if r is None:
            r = CompanyProfileRow(id=1)
            s.add(r)
        for k in (
            "name", "ceo", "biz_num", "tech_stack", "team_size",
            "typical_budget_min", "typical_budget_max",
            "intro_text", "brochure_path", "differentiators",
            "reference_proposal_text", "reference_proposal_path", "reference_instructions",
            "pptx_master_1_path", "pptx_master_1_label",
            "pptx_master_2_path", "pptx_master_2_label",
            "pptx_master_3_path", "pptx_master_3_label",
            "pptx_master_4_path", "pptx_master_4_label",
        ):
            if k in data:
                setattr(r, k, data[k])
        r.updated_at = datetime.now()


def get_proposal_toc(bid_id: str) -> Optional[list[dict]]:
    with session_scope() as s:
        r = s.get(ProposalTocRow, bid_id)
        return r.sections if r else None


def get_proposal_toc_global_context(bid_id: str) -> dict:
    with session_scope() as s:
        r = s.get(ProposalTocRow, bid_id)
        if r is None:
            return {}
        return getattr(r, "global_context", None) or {}


def save_proposal_toc(
    bid_id: str,
    sections: list[dict],
    global_context: Optional[dict] = None,
) -> None:
    with session_scope() as s:
        r = s.get(ProposalTocRow, bid_id)
        if r is None:
            r = ProposalTocRow(bid_id=bid_id)
            s.add(r)
        r.sections = sections
        if global_context is not None:
            r.global_context = global_context
        r.updated_at = datetime.now()


# ---------------------------------------------------------------------------
# 레퍼런스 제안서 라이브러리
# ---------------------------------------------------------------------------

def list_reference_proposals(limit: Optional[int] = None) -> list[dict]:
    with session_scope() as s:
        stmt = select(ReferenceProposalRow).order_by(ReferenceProposalRow.id.desc())
        if limit:
            stmt = stmt.limit(limit)
        rows = s.execute(stmt).scalars().all()
        return [
            {
                "id": r.id,
                "title": r.title,
                "client": r.client,
                "domain": r.domain,
                "won": r.won,
                "body_text": r.body_text or "",
                "file_path": r.file_path,
                "instructions": r.instructions or "",
                "created_at": r.created_at,
            }
            for r in rows
        ]


def add_reference_proposal(data: dict) -> int:
    with session_scope() as s:
        r = ReferenceProposalRow(
            title=data.get("title") or "(무제)",
            client=data.get("client") or "",
            domain=data.get("domain") or "",
            won=int(data.get("won", 1)),
            body_text=data.get("body_text") or "",
            file_path=data.get("file_path"),
            instructions=data.get("instructions") or "",
        )
        s.add(r)
        s.flush()
        return r.id


def delete_reference_proposal(ref_id: int) -> None:
    with session_scope() as s:
        r = s.get(ReferenceProposalRow, ref_id)
        if r:
            s.delete(r)


def pick_relevant_references(domain_keywords: list[str], top_k: int = 2) -> list[dict]:
    """공고 도메인 키워드와 가장 유사한 레퍼런스 top-k.
    매우 단순한 키워드 매칭 (수주성공 우선)."""
    refs = list_reference_proposals()
    if not refs:
        return []
    def score(r):
        text = f"{r.get('domain','')} {r.get('title','')} {r.get('client','')}".lower()
        kw_hits = sum(1 for kw in domain_keywords if kw and kw.lower() in text)
        return (r.get("won") or 0) * 10 + kw_hits
    refs.sort(key=score, reverse=True)
    return refs[:top_k]


# ---------------------------------------------------------------------------
# 회사 자산 카탈로그 (솔루션/실적/인증/수치)
# ---------------------------------------------------------------------------

ASSET_KINDS = ("solution", "case", "cert", "metric")


def list_company_assets(kind: Optional[str] = None) -> list[dict]:
    with session_scope() as s:
        stmt = select(CompanyAssetRow).order_by(CompanyAssetRow.id.desc())
        if kind:
            stmt = stmt.where(CompanyAssetRow.kind == kind)
        rows = s.execute(stmt).scalars().all()
        return [
            {
                "id": r.id,
                "kind": r.kind,
                "title": r.title,
                "body": r.body or "",
                "extra": r.extra or {},
                "created_at": r.created_at,
            }
            for r in rows
        ]


def add_company_asset(kind: str, title: str, body: str, extra: Optional[dict] = None) -> int:
    if kind not in ASSET_KINDS:
        raise ValueError(f"unknown asset kind: {kind}")
    with session_scope() as s:
        r = CompanyAssetRow(kind=kind, title=title, body=body or "", extra=extra or {})
        s.add(r)
        s.flush()
        return r.id


def delete_company_asset(asset_id: int) -> None:
    with session_scope() as s:
        r = s.get(CompanyAssetRow, asset_id)
        if r:
            s.delete(r)


def log_activity(agent_key: str, event: str, message: str, bid_id: Optional[str] = None) -> None:
    with session_scope() as s:
        s.add(ActivityRow(
            agent_key=agent_key,
            event=event,
            bid_id=bid_id,
            message=message,
        ))


def list_activity(limit: int = 50) -> list[dict]:
    with session_scope() as s:
        rows = s.execute(
            select(ActivityRow).order_by(ActivityRow.id.desc()).limit(limit)
        ).scalars().all()
        return [
            {
                "agent_key": r.agent_key,
                "event": r.event,
                "bid_id": r.bid_id,
                "message": r.message,
                "created_at": r.created_at,
            }
            for r in rows
        ]


def list_audit(limit: int = 100) -> list[dict]:
    with session_scope() as s:
        rows = s.execute(
            select(AuditRow).order_by(AuditRow.id.desc()).limit(limit)
        ).scalars().all()
        return [
            {
                "agent_name": r.agent_name,
                "model": r.model,
                "tokens_in": r.tokens_in,
                "tokens_out": r.tokens_out,
                "cost_usd": r.cost_usd,
                "bid_id": r.bid_id,
                "note": r.note,
                "created_at": r.created_at,
            }
            for r in rows
        ]


def list_proposals(bid_id: Optional[str] = None) -> list[dict]:
    with session_scope() as s:
        stmt = select(ProposalRow).order_by(ProposalRow.id.desc())
        if bid_id:
            stmt = stmt.where(ProposalRow.bid_id == bid_id)
        rows = s.execute(stmt).scalars().all()
        return [
            {
                "id": r.id,
                "bid_id": r.bid_id,
                "version": r.version,
                "docx_path": r.docx_path,
                "pptx_path": r.pptx_path,
                "storyboard_path": r.storyboard_path,
                "script_path": r.script_path,
                "generated_at": r.generated_at,
                "section_count": len(r.sections or []),
            }
            for r in rows
        ]


def list_quality_reports(bid_id: Optional[str] = None) -> list[dict]:
    with session_scope() as s:
        stmt = select(QualityRow).order_by(QualityRow.id.desc())
        if bid_id:
            stmt = stmt.where(QualityRow.bid_id == bid_id)
        rows = s.execute(stmt).scalars().all()
        return [
            {
                "bid_id": r.bid_id,
                "version": r.version,
                "overall_grade": r.overall_grade,
                "spelling_count": len(r.spelling_issues or []),
                "consistency_count": len(r.consistency_issues or []),
                "action_items": r.action_items or [],
                "reviewed_at": r.reviewed_at,
            }
            for r in rows
        ]


def log_audit(entry: AuditLogEntry) -> None:
    with session_scope() as s:
        s.add(AuditRow(
            agent_name=entry.agent_name,
            model=entry.model,
            tokens_in=entry.tokens_in,
            tokens_out=entry.tokens_out,
            cost_usd=entry.cost_usd,
            bid_id=entry.bid_id,
            note=entry.note,
            created_at=entry.created_at,
        ))


def _row_to_bid(r: BidRow) -> BidNotice:
    return BidNotice(
        bid_id=r.bid_id,
        source=r.source,
        agency=r.agency,
        title=r.title,
        budget_krw=r.budget_krw,
        duration_months=r.duration_months,
        qualifications=r.qualifications or [],
        deadline=r.deadline,
        rfp_summary=r.rfp_summary or "",
        rfp_url=r.rfp_url or "",
        rfp_full_text=r.rfp_full_text,
        collected_at=r.collected_at,
    )
