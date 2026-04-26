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

# 프로젝트 루트
ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT_DIR / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)

STORAGE_DIR = ROOT_DIR / "storage"
OUTPUT_DIR = STORAGE_DIR / "outputs"
DB_PATH = STORAGE_DIR / "db.sqlite"
STORAGE_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default) or default


def _int_env(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


@dataclass
class CompanyProfile:
    """회사 기본정보. 박제안 에이전트가 회사소개 섹션 작성에 사용."""
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
    """에이전트별 모델 매핑. Replit AI Integrations가 지원하는 모델만 사용."""
    kim: str = field(default_factory=lambda: _env("MODEL_KIM", "gpt-4o-mini"))
    lee: str = field(default_factory=lambda: _env("MODEL_LEE", "claude-haiku-4-5"))
    park: str = field(default_factory=lambda: _env("MODEL_PARK", "claude-opus-4-7"))
    choi: str = field(default_factory=lambda: _env("MODEL_CHOI", "claude-sonnet-4-6"))
    oh: str = field(default_factory=lambda: _env("MODEL_OH", "gpt-4o-mini"))


@dataclass
class IntegrationKeys:
    """외부 서비스 키. 미설정 시 fallback 동작."""
    g2b_service_key: str = field(default_factory=lambda: _env("G2B_SERVICE_KEY"))
    notion_api_key: str = field(default_factory=lambda: _env("NOTION_API_KEY"))
    notion_db_bids: str = field(default_factory=lambda: _env("NOTION_DB_BIDS"))
    notion_db_proposals: str = field(default_factory=lambda: _env("NOTION_DB_PROPOSALS"))
    slack_webhook: str = field(default_factory=lambda: _env("SLACK_WEBHOOK_URL"))

    @property
    def has_g2b(self) -> bool:
        return bool(self.g2b_service_key)

    @property
    def has_notion(self) -> bool:
        return bool(self.notion_api_key and self.notion_db_bids)


@dataclass
class LLMEndpoints:
    """Replit AI Integrations 엔드포인트. 자동 주입된 env 사용."""
    anthropic_base_url: str = field(
        default_factory=lambda: _env("AI_INTEGRATIONS_ANTHROPIC_BASE_URL")
    )
    anthropic_api_key: str = field(
        default_factory=lambda: _env("AI_INTEGRATIONS_ANTHROPIC_API_KEY")
    )
    openai_base_url: str = field(
        default_factory=lambda: _env("AI_INTEGRATIONS_OPENAI_BASE_URL")
    )
    openai_api_key: str = field(
        default_factory=lambda: _env("AI_INTEGRATIONS_OPENAI_API_KEY")
    )

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_base_url and self.anthropic_api_key)

    @property
    def has_openai(self) -> bool:
        return bool(self.openai_base_url and self.openai_api_key)


@dataclass
class SystemConfig:
    fit_score_threshold: int = field(
        default_factory=lambda: _int_env("FIT_SCORE_THRESHOLD", 70)
    )
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    environment: str = field(default_factory=lambda: _env("ENVIRONMENT", "development"))
    monthly_budget_usd: int = field(
        default_factory=lambda: _int_env("MONTHLY_BUDGET_USD", 200)
    )


# 싱글턴 인스턴스
COMPANY = CompanyProfile()
MODELS = ModelConfig()
KEYS = IntegrationKeys()
LLM = LLMEndpoints()
SYS = SystemConfig()


def apply_agent_overrides() -> None:
    """DB에 저장된 대시보드 설정을 MODELS 싱글턴에 반영.
    매 파이프라인 실행 직전에 호출하여 사장이 변경한 모델을 즉시 적용한다.
    """
    try:
        from tools.db import get_agent_overrides  # 순환 import 방지
        overrides = get_agent_overrides()
    except Exception:
        return
    for key in ("kim", "lee", "park", "choi", "oh"):
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
    """김탐정 수집 키워드. DB → ENV → 기본값 순."""
    try:
        from tools.db import get_agent_overrides
        kws = get_agent_overrides().get("kim", {}).get("keywords", [])
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
            # 추가 정보 동적 부착 (dataclass 필드는 아니지만 박제안이 사용)
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
        f"Anthropic 사용 가능: {LLM.has_anthropic}",
        f"OpenAI 사용 가능: {LLM.has_openai}",
        f"G2B 키 등록: {KEYS.has_g2b}",
        f"Notion 사용 가능: {KEYS.has_notion}",
        f"DB 경로: {DB_PATH}",
        f"산출물 경로: {OUTPUT_DIR}",
    ]
    return "\n".join(lines)
