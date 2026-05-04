"""
전역 설정 모듈.
환경변수, 모델 매핑, 회사 정보 등을 한 곳에서 로드한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT_DIR / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)

STORAGE_DIR = ROOT_DIR / "storage"
OUTPUT_DIR = STORAGE_DIR / "outputs"
MASTERS_DIR = STORAGE_DIR / "masters"
PROPOSALS_DIR = STORAGE_DIR / "proposals"
RFP_CACHE_DIR = STORAGE_DIR / "rfp_cache"
DB_PATH = STORAGE_DIR / "db.sqlite"

DATA_DIR = ROOT_DIR / "data"
PROCESSED_DIR = DATA_DIR / "processed"
CHROMA_DB_PATH = DATA_DIR / "chroma_db"
WINNING_PROPOSALS_DIR = DATA_DIR / "winning_proposals"
WINNING_PROPOSALS_PPTX_DIR = DATA_DIR / "winning_proposals_pptx"
TEMPLATES_DIR = ROOT_DIR / "templates"
PATTERNS_DIR = TEMPLATES_DIR / "patterns"

for _d in (
    STORAGE_DIR, OUTPUT_DIR, MASTERS_DIR, PROPOSALS_DIR, RFP_CACHE_DIR,
    PROCESSED_DIR, CHROMA_DB_PATH,
    PATTERNS_DIR / "horizontal", PATTERNS_DIR / "vertical",
):
    _d.mkdir(parents=True, exist_ok=True)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default) or default


def _int_env(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


@dataclass
class CompanyProfile:
    """회사 기본정보. WriterAgent가 회사소개 섹션 작성에 사용."""
    name: str = field(default_factory=lambda: _env("COMPANY_NAME", "주식회사 예시웹에이전시"))
    ceo: str = field(default_factory=lambda: _env("COMPANY_CEO", "우진"))
    biz_num: str = field(default_factory=lambda: _env("COMPANY_BIZ_NUM", "000-00-00000"))
    tech_stack: List[str] = field(
        default_factory=lambda: _env(
            "COMPANY_TECH_STACK",
            "React,Next.js,Node.js,Python,AWS,PostgreSQL",
        ).split(",")
    )
    team_size: int = field(default_factory=lambda: _int_env("COMPANY_TEAM_SIZE", 15))
    typical_budget_min: int = field(
        default_factory=lambda: _int_env("COMPANY_TYPICAL_BUDGET_MIN", 30_000_000)
    )
    typical_budget_max: int = field(
        default_factory=lambda: _int_env("COMPANY_TYPICAL_BUDGET_MAX", 500_000_000)
    )


@dataclass
class ModelConfig:
    """에이전트별 모델 매핑.

    모델 차등 적용 원칙:
      - discovery  : claude-haiku-4-5    (단순 분류/수집, 비용 최소화)
      - analysis   : claude-sonnet-4-6   (정확도 중심 평가)
      - rfp_struct : claude-sonnet-4-6   (RFP 구조화)
      - strategy   : claude-opus-4-7     (창의성, 전략 수립)
      - writer     : claude-opus-4-7     (품질 최우선 글쓰기)
      - reviewer   : claude-sonnet-4-6   (검수)
      - graphics   : claude-sonnet-4-6   (슬라이드 설계)
    """
    discovery: str = field(default_factory=lambda: _env("MODEL_DISCOVERY", "claude-haiku-4-5-20251001"))
    analysis: str = field(default_factory=lambda: _env("MODEL_ANALYSIS", "claude-sonnet-4-6"))
    rfp_struct: str = field(default_factory=lambda: _env("MODEL_RFP_STRUCT", "claude-sonnet-4-6"))
    strategy: str = field(default_factory=lambda: _env("MODEL_STRATEGY", "claude-opus-4-7"))
    writer: str = field(default_factory=lambda: _env("MODEL_WRITER", "claude-opus-4-7"))
    reviewer: str = field(default_factory=lambda: _env("MODEL_REVIEWER", "claude-sonnet-4-6"))
    graphics: str = field(default_factory=lambda: _env("MODEL_GRAPHICS", "claude-sonnet-4-6"))


@dataclass
class IntegrationKeys:
    """외부 서비스 키. 미설정 시 fallback 동작."""
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    voyage_api_key: str = field(default_factory=lambda: _env("VOYAGE_API_KEY"))
    g2b_service_key: str = field(default_factory=lambda: _env("G2B_SERVICE_KEY"))
    notion_api_key: str = field(default_factory=lambda: _env("NOTION_API_KEY"))
    notion_db_bids: str = field(default_factory=lambda: _env("NOTION_DB_BIDS"))
    notion_db_proposals: str = field(default_factory=lambda: _env("NOTION_DB_PROPOSALS"))
    slack_webhook: str = field(default_factory=lambda: _env("SLACK_WEBHOOK_URL"))
    aws_access_key_id: str = field(default_factory=lambda: _env("AWS_ACCESS_KEY_ID"))
    aws_secret_access_key: str = field(default_factory=lambda: _env("AWS_SECRET_ACCESS_KEY"))
    aws_region: str = field(default_factory=lambda: _env("AWS_REGION", "ap-northeast-1"))

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_voyage(self) -> bool:
        return bool(self.voyage_api_key)

    @property
    def has_g2b(self) -> bool:
        return bool(self.g2b_service_key)

    @property
    def has_notion(self) -> bool:
        return bool(self.notion_api_key and self.notion_db_bids)

    @property
    def has_bedrock(self) -> bool:
        return bool(
            self.aws_access_key_id
            and self.aws_secret_access_key
            and self.aws_region
        )


@dataclass
class VectorStoreConfig:
    """ChromaDB + Voyage AI 벡터 스토어 설정."""
    chroma_persist_dir: str = field(
        default_factory=lambda: str(CHROMA_DB_PATH)
    )
    voyage_model: str = field(
        default_factory=lambda: _env("VOYAGE_MODEL", "voyage-multilingual-2")
    )
    embedding_batch_size: int = field(
        default_factory=lambda: _int_env("EMBEDDING_BATCH_SIZE", 128)
    )
    collection_rfp: str = "rfp_documents"
    collection_proposals: str = "reference_proposals"
    collection_assets: str = "company_assets"


@dataclass
class SystemConfig:
    fit_score_threshold: int = field(
        default_factory=lambda: _int_env("FIT_SCORE_THRESHOLD", 70)
    )
    toc_similarity_threshold: float = field(
        default_factory=lambda: float(_env("TOC_SIMILARITY_THRESHOLD", "0.90"))
    )
    target_pages_min: int = field(
        default_factory=lambda: _int_env("TARGET_PAGES_MIN", 70)
    )
    target_pages_max: int = field(
        default_factory=lambda: _int_env("TARGET_PAGES_MAX", 120)
    )
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    environment: str = field(default_factory=lambda: _env("ENVIRONMENT", "development"))
    monthly_budget_usd: int = field(
        default_factory=lambda: _int_env("MONTHLY_BUDGET_USD", 200)
    )
    use_bedrock: bool = field(
        default_factory=lambda: _env("USE_BEDROCK", "false").lower() in ("true", "1", "yes")
    )


# 싱글턴 인스턴스
COMPANY = CompanyProfile()
MODELS = ModelConfig()
KEYS = IntegrationKeys()
VECTOR = VectorStoreConfig()
SYS = SystemConfig()


def apply_agent_overrides() -> None:
    """DB에 저장된 대시보드 설정을 MODELS 싱글턴에 반영."""
    try:
        from tools.db import get_agent_overrides
        overrides = get_agent_overrides()
    except Exception:
        return
    for key in ("discovery", "analysis", "rfp_struct", "strategy", "writer", "reviewer", "graphics"):
        cfg = overrides.get(key)
        if cfg and cfg.get("model"):
            setattr(MODELS, key, cfg["model"])


def get_extra_instructions(agent_key: str) -> str:
    try:
        from tools.db import get_agent_overrides
        return get_agent_overrides().get(agent_key, {}).get("extra_instructions", "")
    except Exception:
        return ""


def get_collection_keywords() -> list[str]:
    """DiscoveryAgent 수집 키워드. DB → ENV → 기본값 순."""
    try:
        from tools.db import get_agent_overrides
        kws = get_agent_overrides().get("discovery", {}).get("keywords", [])
        if kws:
            return kws
    except Exception:
        pass
    raw = _env("COLLECT_KEYWORDS", "홈페이지,웹사이트,시스템 구축,플랫폼,포털")
    return [k.strip() for k in raw.split(",") if k.strip()]


def get_effective_company() -> CompanyProfile:
    """DB에 저장된 회사 정보가 있으면 그것을, 없으면 .env 기본값을 반환."""
    try:
        from tools.db import get_company_profile
        d = get_company_profile()
        if d and d.get("name"):
            cp = CompanyProfile(
                name=d["name"],
                ceo=d["ceo"] or "",
                biz_num=d["biz_num"] or "",
                tech_stack=d["tech_stack"] or [],
                team_size=d["team_size"] or 0,
                typical_budget_min=d["typical_budget_min"] or 0,
                typical_budget_max=d["typical_budget_max"] or 0,
            )
            cp.intro_text = d.get("intro_text", "")  # type: ignore
            cp.differentiators = d.get("differentiators", "")  # type: ignore
            cp.brochure_path = d.get("brochure_path", "")  # type: ignore
            cp.reference_proposal_text = d.get("reference_proposal_text", "")  # type: ignore
            cp.reference_proposal_path = d.get("reference_proposal_path", "")  # type: ignore
            cp.reference_instructions = d.get("reference_instructions", "")  # type: ignore
            return cp
    except Exception:
        pass
    cp = COMPANY
    cp.intro_text = ""  # type: ignore
    cp.differentiators = ""  # type: ignore
    cp.brochure_path = ""  # type: ignore
    cp.reference_proposal_text = ""  # type: ignore
    cp.reference_proposal_path = ""  # type: ignore
    cp.reference_instructions = ""  # type: ignore
    return cp


def summary() -> str:
    """현재 설정 요약 (CLI 진단용)."""
    lines = [
        f"환경: {SYS.environment}",
        f"회사: {COMPANY.name} (대표 {COMPANY.ceo})",
        f"점수 임계값: {SYS.fit_score_threshold}",
        f"목차 유사도 임계값: {SYS.toc_similarity_threshold}",
        f"목표 페이지: {SYS.target_pages_min}~{SYS.target_pages_max}",
        f"Anthropic API: {KEYS.has_anthropic}",
        f"Bedrock 사용: {SYS.use_bedrock} (AWS keys: {KEYS.has_bedrock}, region={KEYS.aws_region})",
        f"Voyage AI: {KEYS.has_voyage}",
        f"G2B 키 등록: {KEYS.has_g2b}",
        f"Notion 사용 가능: {KEYS.has_notion}",
        f"DB 경로: {DB_PATH}",
        f"벡터 DB: {VECTOR.chroma_persist_dir}",
        f"산출물 경로: {OUTPUT_DIR}",
    ]
    return "\n".join(lines)
