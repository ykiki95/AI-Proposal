"""
박제안 팀 - 팀장 + 8명 전문 서브 에이전트.

100페이지급 본격 제안서 대응을 위한 분업 구조.
- 팀장: 공고 RFP 분석 → 가변 목차(TOC) 기획 → 각 전문가에게 위임 → 통합
- 전문가 8명: 각자 자기 섹션만 깊이 작성. 분량/페이지 가변.

각 전문가는 자기 섹션과 관련된 회사 자산(솔루션/실적/인증/수치)과
유사 도메인의 레퍼런스 제안서 발췌를 자동 주입받는다.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from loguru import logger

SLIDES_MARKER = "===SLIDES_JSON==="

from config.settings import (
    MODELS,
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
from tools import db
from tools.docx_generator import render_proposal_docx
from tools.llm_clients import chat_anthropic


# ---------------------------------------------------------------------------
# 전문가 카탈로그
# ---------------------------------------------------------------------------

@dataclass
class Specialist:
    key: str               # 식별자
    name: str              # 친근한 별칭
    avatar: str
    role: str              # 한 줄 역할
    is_critical: bool      # 핵심 3인 여부 (To-Be 화면 강조)
    default_pages: int     # 기본 페이지 분량
    system_prompt: str
    asset_kinds: Tuple[str, ...] = field(default_factory=tuple)
    use_reference: bool = True  # 레퍼런스 제안서 발췌 주입 여부


_SLIDE_SPEC_INSTRUCTION = """

[★ 출력 마지막 — 슬라이드 스펙 JSON]
본문 마지막에 반드시 한 줄에 다음 마커를 적은 후, 슬라이드 스펙 JSON 배열을 출력한다.
마커: ===SLIDES_JSON===

JSON 배열의 각 원소는 한 장의 PPT 슬라이드 스펙이며, 본인 섹션의 본문을 4~10장으로 분할한다.
사용 가능한 layout 종류와 필요한 필드:
- {"layout":"section_divider","title":"섹션 큰 제목","subtitle":"한 줄 부제"}
- {"layout":"title_bullets","title":"...","bullets":["...", "...", "..."]}
- {"layout":"two_column_compare","title":"AS-IS / TO-BE","left":{"label":"AS-IS","items":["...","..."]},"right":{"label":"TO-BE","items":["...","..."]}}
- {"layout":"diagram_layered","title":"시스템 구성도","diagram":{"layers":[{"name":"사용자","items":["웹","모바일"]},{"name":"API","items":["Gateway","Auth"]},{"name":"데이터","items":["DB","캐시"]}]}}
- {"layout":"metric_cards","title":"핵심 정량","metrics":[{"value":"90.05%","label":"음성인식 정확도","note":"GS인증"},{"value":"-45%","label":"행정 처리시간"}]}
- {"layout":"table","title":"...","table_data":{"headers":["연도","발주처","사업명"],"rows":[["2024","경찰청","..."]]}}
- {"layout":"as_is_to_be_compare","title":"현행 vs 개선 후","as_is_items":["현행 문제 1","현행 문제 2","현행 문제 3"],"to_be_items":["개선 후 모습 1","개선 후 모습 2","개선 후 모습 3"],"arrow_label":"전환 핵심 한 줄"}
- {"layout":"system_architecture","title":"시스템 아키텍처","architecture":{"layers":[{"name":"사용자","items":["응시자 PC","감독관 PC","모바일"]},{"name":"프론트","items":["Web","Mobile App"]},{"name":"API","items":["Gateway","Auth","Score API"]},{"name":"데이터","items":["RDB","Cache","Object Storage"]},{"name":"AI","items":["부정탐지","채점"]}],"external":["국세청","행안부","교육부"]}}
- {"layout":"process_flow","title":"단계별 추진 절차","flow_steps":[{"step":"1","title":"분석","desc":"현행 진단·요구 정의"},{"step":"2","title":"설계","desc":"AS-IS/TO-BE 확정"},{"step":"3","title":"개발","desc":"모듈별 구현"},{"step":"4","title":"이행","desc":"안정화·교육"}]}
- {"layout":"screen_mockup","title":"To-Be 화면 — 응시자 메인","mockup":{"device":"pc","header":"TOPIK 응시자 포털","footer":"© 2026","label":"응시자 메인","regions":[{"label":"공지·이벤트 배너","kind":"hero"},{"label":"내 시험 일정 카드 (시험일·고사장·QR)","kind":"panel"},{"label":"빠른 메뉴 4개 (접수/성적/증명서/문의)","kind":"grid"},{"label":"FAQ 미리보기","kind":"list"}]}}
- {"layout":"screen_mockup_grid","title":"화면 모음 — 4종","mockups":[{"label":"응시자 메인","device":"pc","header":"메인","regions":[{"label":"히어로","kind":"hero"},{"label":"공지","kind":"list"}]},{"label":"감독관 콘솔","device":"pc","header":"감독관","regions":[{"label":"실시간 부정탐지","kind":"chart"},{"label":"좌석 배치","kind":"grid"}]},{"label":"성적 조회","device":"mobile","header":"성적","regions":[{"label":"내 점수","kind":"hero"},{"label":"증명서","kind":"panel"}]},{"label":"관리자 대시보드","device":"pc","header":"관리자","regions":[{"label":"KPI","kind":"chart"},{"label":"통계 표","kind":"table"}]}]}

규칙:
- 본인 섹션 첫 슬라이드는 거의 항상 section_divider 1장.
- AS-IS/TO-BE를 다룰 때는 두꺼운 텍스트 박스(title_bullets·two_column_compare) 대신 `as_is_to_be_compare` 도형을 우선 사용.
- 시스템 구성·아키텍처를 다룰 때는 `system_architecture`를 우선 사용.
- 단계·절차·로드맵은 `process_flow`를 우선 사용.
- 화면을 1장 자세히 보여줄 때는 `screen_mockup`, 여러 화면 일람은 `screen_mockup_grid`.
- regions의 kind는 panel|grid|list|hero|table|chart 중 선택. label은 12자 내외 한글로 구체적으로.
- 표·다이어그램·정량은 본문에 있을 때만 만들고, 거짓 수치 금지.
- 각 슬라이드 speaker_notes 필드(선택, 1~2문장)에 발표 시 강조할 점 적기.
- JSON만 출력 (설명 금지). 마크다운 코드블록(```) 사용 금지.
"""


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
3. **To-Be 화면 mockup — 핵심 기능마다 `screen_mockup` 또는 `screen_mockup_grid` 1~2장씩, 본 섹션 합계 최소 3장 이상.**
   각 mockup의 regions에는 라벨을 12자 이내로 구체적으로 넣어 평가위원이 한눈에 화면 구조를 이해하게 한다.
4. 우리 회사만의 차별화 포인트(다른 경쟁사가 흉내낼 수 없는 부분)를 별도 단락 [차별화 포인트]로 명시한다.
5. 가능한 곳마다 자사 솔루션·자산을 자연스럽게 인용한다 (있을 때).
"""

_MOCKUP_INSTRUCTION = """

[추가 — 화면 mockup 강제]
본 섹션은 발주처가 '실제로 어떻게 생긴 화면인지' 보고 싶어하는 영역이다.
SLIDES_JSON에 `screen_mockup` 또는 `screen_mockup_grid` 레이아웃을 합계 5장 이상 포함하라.
- PC 화면(device="pc")과 모바일 화면(device="mobile")을 섞어 사용.
- regions의 kind는 hero/panel/grid/list/table/chart 중 적절한 것을 선택해 화면 다양성을 보여줄 것.
- 각 mockup의 label은 "응시자 메인", "감독관 콘솔" 같이 사용자/역할 + 화면 목적이 보이도록.
"""


SPECIALISTS: List[Specialist] = [
    Specialist(
        key="business",
        name="김이해",
        avatar="🎯",
        role="사업 배경/목적/As-Is·To-Be 분석",
        is_critical=True,
        default_pages=12,
        asset_kinds=("metric", "case"),
        system_prompt=(
            "당신은 공공·민간 SI 사업의 본질적 문제 정의에 능한 시니어 전략 컨설턴트이다.\n"
            "발주처가 RFP에 적은 'stated need'와 그 이면의 'real need'(현장 고통, 정치적 KPI, 후속 확장 의도)를\n"
            "정확히 분리해서 짚어내고, 우리가 그 둘을 모두 해결한다는 서사를 만든다.\n\n"
            "이 섹션에서 반드시 다룰 것:\n"
            "- 사업의 사회적·정책적 배경 (관련 법령·국정과제·지자체 비전 인용 가능 시 인용)\n"
            "- 발주처가 처한 As-Is 문제 진단 (현장 페인포인트 3~5가지, 가능하면 정량 추정치)\n"
            "- To-Be 청사진 (1줄 비전 + 핵심 변화 3가지)\n"
            "- 우리 회사가 이 사업의 '적임자'인 이유 (3가지)\n"
            "- 발주처 입장에서 '지금 당장의 가치'와 '차기 확장 가치'를 동시에 보여주는 서사\n"
            + _BASE_TONE + _VISUAL_INSTRUCTION
        ),
    ),
    Specialist(
        key="solution",
        name="이솔루",
        avatar="🏗️",
        role="시스템 구성도 / 기술 스택 / To-Be 화면",
        is_critical=True,
        default_pages=20,
        asset_kinds=("solution", "metric", "cert"),
        system_prompt=(
            "당신은 웹·모바일·클라우드·AI 인프라를 두루 다룰 수 있는 시니어 SA(Solution Architect)이다.\n"
            "단순한 기술 카탈로그가 아니라, '왜 이 조합이 이 사업에 최적인가'를 논증한다.\n\n"
            "이 섹션에서 반드시 다룰 것:\n"
            "- (필수) 서비스 아키텍처 다이어그램 — 사용자/프론트/백엔드/데이터/AI/외부연계 계층별 텍스트 박스\n"
            "- (필수) 서버·인프라 아키텍처 다이어그램 — 클라우드 리전/AZ, LB, 웹/WAS/DB 서버, 캐시, 백업, CDN, 보안그룹, 배포 토폴로지(Blue-Green/Canary 등) 명시\n"
            "- (필수) 운영 환경 구분(개발/스테이징/운영) 및 가용성·DR 구성\n"
            "- 핵심 기술 스택 선정 근거 표 (대안 vs 선정안 vs 채택 이유)\n"
            "- 자사 솔루션·엔진 활용 (제공된 자산이 있으면 반드시 명시 — 예: 'OOO 엔진 적용')\n"
            "- 데이터 모델 / 핵심 API 설계 개요\n"
            "- 보안·성능·확장성 고려사항 (구체 수치: TPS, 응답시간, 동시접속)\n"
            "- 외부 연계(Integration Hub) 흐름도 — '연계 갯수가 아니라 현장 업무흐름에서 실제로 이어지는가'를 강조\n"
            "- AI/자동화가 들어간다면 '창의성이 아닌 생산성'을 강조하는 서사\n"
            + _BASE_TONE + _VISUAL_INSTRUCTION + _MOCKUP_INSTRUCTION
        ),
    ),
    Specialist(
        key="methodology",
        name="최아키",
        avatar="📐",
        role="단계별 구축 방법론 / 프로세스",
        is_critical=True,
        default_pages=15,
        asset_kinds=("case",),
        system_prompt=(
            "당신은 PMP·CMMI·Agile·RUP 방법론을 사업 특성에 맞게 재단할 줄 아는 시니어 PM이다.\n\n"
            "이 섹션에서 반드시 다룰 것:\n"
            "- 적용 방법론 선정 근거 (Waterfall vs Iterative vs Hybrid — 본 사업에는 왜 X인가)\n"
            "- 분석/설계/개발/테스트/이행/안정화 단계별 핵심 활동·산출물·검증방법·게이트(승인기준) 표\n"
            "- 단계별 작업 흐름 다이어그램 (텍스트 박스로)\n"
            "- 변경 관리·이슈 관리·리스크 관리 프로세스\n"
            "- 발주처와의 의사소통 체계 (정기 회의 주기·보고 방식·승인 절차)\n"
            "- '현장 경찰이 실제로 쓰게 만들기' 같은 사용자 정착(Adoption) 전략 — 교육·매뉴얼·헬프데스크\n"
            + _BASE_TONE + _VISUAL_INSTRUCTION
        ),
    ),
    Specialist(
        key="schedule",
        name="윤관리",
        avatar="📅",
        role="WBS / 일정 / 리스크 관리",
        is_critical=False,
        default_pages=8,
        asset_kinds=(),
        system_prompt=(
            "당신은 PMP 자격을 보유하고 100억 규모 SI 일정을 직접 짜본 PM이다.\n\n"
            "이 섹션에서 반드시 다룰 것:\n"
            "- 전체 사업기간을 4~6개 마일스톤으로 분할한 마스터 스케줄 표\n"
            "- WBS (Work Breakdown Structure) — 3레벨까지, 표 형식\n"
            "- Gantt 형식 일정표 (텍스트 막대로 월차 표시)\n"
            "- 리스크 매트릭스 — 발생가능성(H/M/L) × 영향도(H/M/L), 각 리스크별 사전대응·발생시대응\n"
            "- 일정 단축 옵션 (Crashing / Fast Tracking) 검토\n"
            "- 발주처 검수 일정과 우리 내부 QA 일정의 정렬\n"
            + _BASE_TONE
        ),
    ),
    Specialist(
        key="organization",
        name="(빈 페이지) 조직·인력",
        avatar="👥",
        role="투입 조직 / 핵심 인력 이력",
        is_critical=False,
        default_pages=10,
        asset_kinds=("cert",),
        system_prompt=(
            "당신은 SI 조직 운영과 인력 매칭에 베테랑이다.\n\n"
            "이 섹션에서 반드시 다룰 것:\n"
            "- 사업 전담 조직도 (텍스트 트리 — PM 아래 PL/개발/디자인/QA/인프라/AI 등)\n"
            "- 직무별 R&R 표 (역할 / 핵심 책임 / 산출물 / 투입률)\n"
            "- 핵심 인력(PM/PL 등)의 강점·경력연수·자격증·유사 프로젝트 경험 — 제공된 인력 정보 범위 안에서만\n"
            "- 발주처와의 협업 체계 (PMO / 운영위원회 / TFT)\n"
            "- 인력 백업·이탈 대응 체계\n"
            "회사가 보유한 정보 범위 안에서만 인력을 기술한다 (가공의 인물·이력 금지).\n"
            + _BASE_TONE
        ),
    ),
    Specialist(
        key="company",
        name="(빈 페이지) 회사·실적",
        avatar="🏢",
        role="회사 소개 / 유사 실적",
        is_critical=False,
        default_pages=10,
        asset_kinds=("case", "cert", "metric", "solution"),
        system_prompt=(
            "당신은 회사 소개 카피라이터이자 실적 정리 전문가이다.\n\n"
            "이 섹션에서 반드시 다룰 것:\n"
            "- 회사 한 줄 정체성 (Tagline) — 기억에 남는 한 문장\n"
            "- 회사 연혁·규모·핵심 역량 (제공된 회사 정보 기반)\n"
            "- 보유 인증·자격 표 (제공된 cert 자산 인용)\n"
            "- 자사 솔루션·엔진 카탈로그 — 각 솔루션별 한 줄 소개 + 핵심 수치 + 적용 사례\n"
            "- 유사 수행실적 표 (연도 / 발주처 / 사업명 / 규모 / 우리 역할 / 성과) — 제공된 case 자산만 사용\n"
            "- '왜 이 사업도 우리가 잘 할 수 있는가'를 실적 패턴으로 논증\n"
            "거짓 실적·인증·수치를 절대 만들지 않는다. 제공된 자산이 부족하면 '추가 자료 별첨'으로 표기.\n"
            + _BASE_TONE
        ),
    ),
    Specialist(
        key="quality",
        name="한보안",
        avatar="🛡️",
        role="QA / 보안 / 유지보수 방안",
        is_critical=False,
        default_pages=10,
        asset_kinds=("cert", "metric"),
        system_prompt=(
            "당신은 ISMS·ISO27001·전자정부 보안가이드·OWASP Top10에 정통한 시니어 보안 엔지니어이자 QA 리드이다.\n\n"
            "이 섹션에서 반드시 다룰 것:\n"
            "- 품질보증 전략 (V&V, 단위/통합/시스템/인수 테스트 단계별 목표·도구·통과 기준)\n"
            "- 코드 품질 관리 (정적분석·코드리뷰·커버리지 목표)\n"
            "- 보안 통제 — 행정·물리·기술 3계층, 각 계층별 구체 통제 항목 표\n"
            "- 개인정보·민감정보 보호 (가명처리·암호화·접근통제·감사로그)\n"
            "- 운영·유지보수 SLA — 응답시간/복구시간/가용성 목표 수치\n"
            "- 장애 대응 프로세스·비상연락 체계\n"
            "- 우리 회사의 보유 인증·자격(제공된 cert 자산)이 어떻게 신뢰의 근거가 되는지 명시\n"
            + _BASE_TONE
        ),
    ),
    Specialist(
        key="cost",
        name="노효과",
        avatar="💰",
        role="가격 산정 / 계약 조건",
        is_critical=False,
        default_pages=8,
        asset_kinds=(),
        system_prompt=(
            "당신은 SW사업 대가산정 가이드(과기정통부)에 능한 회계·계약 전문가이다.\n\n"
            "이 섹션에서 반드시 다룰 것:\n"
            "- 산정 방법 명시 (FP / 투입공수 / 기능점수 — 본 사업에 적용한 방식)\n"
            "- 인력 등급별 단가 표 (특/고/중/초급 × 직무) — 가이드 기반 합리적 추정\n"
            "- 단계별·직무별 투입 M/M 산정 표\n"
            "- 직접비·간접비·이윤 구성 표\n"
            "- 총사업비가 발주처 예산 대비 합리적 수준임을 설명 (% 비교)\n"
            "- 변경관리·정산·하자보수 등 계약 조건의 합리성 설명\n"
            + _BASE_TONE
        ),
    ),
]


# ---------------------------------------------------------------------------
# 컨텍스트 빌더 — 자산·레퍼런스 자동 주입
# ---------------------------------------------------------------------------

def _extract_first_json_array(text: str) -> Optional[str]:
    """문자열에서 첫 JSON 배열을 brace-count 방식으로 추출."""
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


def _split_body_and_slides(raw: str) -> Tuple[str, List[Dict]]:
    """LLM 원본 응답을 (본문 마크다운, slides_json) 으로 분리."""
    if SLIDES_MARKER not in raw:
        return raw.strip(), []
    body, _, tail = raw.partition(SLIDES_MARKER)
    body = body.strip()
    json_text = _extract_first_json_array(tail)
    if not json_text:
        return body, []
    try:
        arr = json.loads(json_text)
    except Exception as e:
        logger.warning(f"[park_team] slides_json 파싱 실패: {e}")
        return body, []
    out: List[Dict] = []
    for item in arr if isinstance(arr, list) else []:
        if not isinstance(item, dict) or not item.get("layout") or not item.get("title"):
            continue
        out.append(item)
    return body, out


def _placeholder_slides(title: str) -> List[Dict]:
    """사람이 채우는 섹션의 빈 슬라이드 스펙."""
    return [
        {"layout": "section_divider", "title": title, "subtitle": "사장님 직접 작성 영역"},
        {"layout": "blank_placeholder", "title": title,
         "placeholder_reason": "회사 소개·실적·인력 정보는 영업/HR 담당이 직접 채웁니다."},
        {"layout": "blank_placeholder", "title": f"{title} (보충 페이지)",
         "placeholder_reason": "필요 시 1장 더 사용하세요."},
    ]


def _format_assets(kind: str, items: list[dict]) -> str:
    if not items:
        return ""
    label = {
        "solution": "💡 자사 솔루션",
        "case": "🏆 유사 수행실적",
        "cert": "🎖️ 보유 인증·자격",
        "metric": "📊 정량 자산 수치",
    }.get(kind, kind)
    lines = [f"## {label}"]
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


def _format_references(refs: list[dict]) -> str:
    if not refs:
        return ""
    out = ["## 📚 유사 도메인 레퍼런스 제안서 (모방 대상)"]
    for r in refs:
        won = "수주성공" if r.get("won") else "참고용"
        out.append(f"### [{won}] {r.get('title','')} — {r.get('client','')} ({r.get('domain','')})")
        if r.get("instructions"):
            out.append(f"📌 모방 지시: {r['instructions']}")
        body = (r.get("body_text") or "")[:3000]
        if body:
            out.append("---")
            out.append(body)
            out.append("---")
    return "\n".join(out)


def _build_context_block(specialist: Specialist, bid: BidNotice) -> str:
    parts: list[str] = []
    # 자산
    for kind in specialist.asset_kinds:
        items = db.list_company_assets(kind=kind)
        block = _format_assets(kind, items)
        if block:
            parts.append(block)
    # 레퍼런스
    if specialist.use_reference:
        keywords = []
        for src in (bid.title or "", bid.agency or ""):
            keywords.extend([w for w in src.split() if len(w) >= 2])
        refs = db.pick_relevant_references(keywords, top_k=2)
        block = _format_references(refs)
        if block:
            parts.append(block)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 박제안 팀장
# ---------------------------------------------------------------------------

class ParkTeam:
    name = "박제안"
    role = "제안서 팀장 (TOC 기획 + 통합)"

    def _model(self) -> str:
        return MODELS.park

    def plan_toc(self, bid: BidNotice, force_refresh_guides: bool = False) -> List[Dict]:
        """
        목차(TOC) 기획.
        - 저장된 사용자 정의 목차가 있으면 **구조(title/specialty/target_pages/순서)를 보존**하되,
          brief가 비어있는 항목은 RFP 자동 가이드로 보강한다. force_refresh_guides=True면
          brief만 새 가이드로 덮어쓴다 (사용자 구조 보존).
        - 저장된 목차가 없으면 SPECIALISTS 8명 기본 구조에 RFP 가이드 주입 후 저장.
          가이드 추출 실패 시에도 baseline TOC를 저장해 다음 호출의 LLM 재호출을 막는다.
        - 보강 또는 신규 생성 후에는 항상 DB에 저장한다.
        """
        from tools.rfp_section_guide import extract_section_guides

        saved = db.get_proposal_toc(bid.bid_id)

        if saved:
            # saved 구조는 절대 보존. brief만 채우거나 갱신.
            need_guides = force_refresh_guides or any(
                not (s.get("brief") or "").strip() for s in saved
            )
            if not need_guides:
                logger.info(f"[{self.name}] 저장된 목차 {len(saved)}섹션 사용 (가이드 완비)")
                return saved
            guides = extract_section_guides(bid)
            changed = False
            for s in saved:
                key = s.get("specialty") or ""
                g = guides.get(key, "")
                if force_refresh_guides:
                    if g and g != (s.get("brief") or ""):
                        s["brief"] = g
                        changed = True
                else:
                    if not (s.get("brief") or "").strip() and g:
                        s["brief"] = g
                        changed = True
            if changed:
                try:
                    db.save_proposal_toc(bid.bid_id, saved)
                    logger.info(
                        f"[{self.name}] 저장된 목차에 RFP 가이드 보강·저장 ({len(saved)}섹션)"
                    )
                except Exception as e:
                    logger.warning(f"[{self.name}] TOC 저장 실패(무시): {e}")
            else:
                logger.info(
                    f"[{self.name}] 저장된 목차 {len(saved)}섹션 사용 "
                    "(가이드 추출 결과 적용할 변경 없음)"
                )
            return saved

        # saved 없음 → SPECIALISTS 기본 구조로 신규 생성
        guides = extract_section_guides(bid)
        toc = [
            {
                "title": sp.role,
                "specialty": sp.key,
                "target_pages": sp.default_pages,
                "brief": guides.get(sp.key, ""),
            }
            for sp in SPECIALISTS
        ]
        # 가이드 추출 실패해도 baseline은 저장 (LLM 재호출 방지 + race window 축소)
        try:
            db.save_proposal_toc(bid.bid_id, toc)
            logger.info(
                f"[{self.name}] 신규 TOC 저장 ({len(toc)}섹션, 가이드 {sum(1 for t in toc if t['brief'])}/8)"
            )
        except Exception as e:
            logger.warning(f"[{self.name}] TOC 저장 실패(무시): {e}")
        return toc

    # ------------------------------------------------------------------
    # 동적 specialist + 글로벌 컨텍스트 (RFP 목차 추출기 결합)
    # ------------------------------------------------------------------

    def make_dynamic_specialist(self, section_def: Dict) -> Specialist:
        """RFP 목차에서 추출된 챕터 정의를 받아, base specialty 기반의
        동적 호칭(display_role)을 가진 Specialist 인스턴스를 만든다.
        sub_chapters 포함 여부는 호출측에서 별도 처리."""
        base_key = (section_def.get("specialty") or "business").strip()
        base = next((s for s in SPECIALISTS if s.key == base_key), SPECIALISTS[0])
        display_role = (
            (section_def.get("display_role") or "").strip()
            or section_def.get("title")
            or base.role
        )
        # name은 base 유지(전문가 색상·아바타 일관성), role만 동적 교체
        return Specialist(
            key=base.key,
            name=base.name,
            avatar=base.avatar,
            role=display_role,
            is_critical=base.is_critical,
            default_pages=int(section_def.get("target_pages") or base.default_pages),
            system_prompt=base.system_prompt,
            asset_kinds=base.asset_kinds,
            use_reference=base.use_reference,
        )

    def plan_toc_from_rfp(self, bid: BidNotice) -> Tuple[List[Dict], Dict]:
        """RFP 본문을 LLM으로 1회 분석해 '발주처가 명시한 목차 트리'를 추출하고
        글로벌 스토리 컨텍스트와 함께 DB에 저장한다.

        반환: (chapters, global_context). 추출 실패 시 ([], {}).
        """
        from tools.rfp_toc_extractor import extract_rfp_toc

        chapters, global_ctx = extract_rfp_toc(bid)
        if not chapters:
            return [], {}
        try:
            db.save_proposal_toc(bid.bid_id, chapters, global_context=global_ctx)
            logger.info(
                f"[{self.name}] RFP 목차 트리 저장 ({len(chapters)}챕터, "
                f"sub {sum(len(c.get('sub_chapters', [])) for c in chapters)}개)"
            )
        except Exception as e:
            logger.warning(f"[{self.name}] RFP 목차 저장 실패(무시): {e}")
        return chapters, global_ctx

    # ------------------------------------------------------------------
    # 글로벌 컨텍스트 → 모든 sub-호출에 주입할 짧은 텍스트 블록
    # ------------------------------------------------------------------

    def _format_global_context(self, gctx: Dict, current_idx: int = 0,
                                total: int = 0, prev_title: str = "",
                                next_title: str = "") -> str:
        if not gctx:
            return ""
        vision = (gctx.get("vision") or "").strip()
        kws = gctx.get("key_keywords") or []
        diffs = gctx.get("differentiators") or []
        lines = ["[★ 전체 제안 스토리 컨텍스트 — 본 섹션도 여기에 정렬할 것]"]
        if vision:
            lines.append(f"- 한 줄 비전: {vision}")
        if kws:
            lines.append(f"- 핵심 키워드 5개: {', '.join(kws[:5])}")
        if diffs:
            lines.append("- 자사 차별점:")
            for d in diffs[:3]:
                lines.append(f"  · {d}")
        if total:
            lines.append(f"- 본 챕터 위치: 전체 {total}챕터 중 {current_idx + 1}번째")
        if prev_title:
            lines.append(f"- 직전 챕터: {prev_title}")
        if next_title:
            lines.append(f"- 다음 챕터: {next_title} (자연스럽게 이어지도록 마지막 단락에서 가볍게 예고)")
        return "\n".join(lines)

    def _llm_section_call(
        self,
        *,
        bid: BidNotice,
        specialist: Specialist,
        section_title: str,
        section_brief: str,
        target_pages: int,
        version: int,
        global_ctx_block: str,
        is_subchapter: bool = False,
        parent_title: str = "",
    ) -> Tuple[str, List[Dict]]:
        """단일 LLM 호출 — 본문 + slides_json 반환."""
        target_chars = target_pages * 600
        max_tokens = min(8000, max(1500, int(target_chars * 2.5)))

        company = get_effective_company()
        team_extra = get_extra_instructions("park")
        company_intro = getattr(company, "intro_text", "") or ""
        differentiators = getattr(company, "differentiators", "") or ""
        context_block = _build_context_block(specialist, bid)
        version_note = (
            f"# 개정 안내\n버전 v{version} (수정 지시 반영)" if version > 1 else ""
        )
        sub_note = ""
        if is_subchapter and parent_title:
            sub_note = (
                f"\n# 본 호출은 상위 챕터 '{parent_title}'의 sub-챕터 작성이다.\n"
                "- 상위 챕터 전체 흐름 안에서 본인 sub-챕터만 책임지고 작성한다.\n"
                "- section_divider 슬라이드는 만들지 말고 본문 슬라이드부터 시작한다 (상위 간지는 따로 들어간다).\n"
            )

        user_prompt = f"""# 작성 섹션
- 제목: {section_title}
- 담당 전문가: {specialist.avatar} {specialist.name} ({specialist.role})
- 목표 분량: 약 {target_pages}페이지 ({target_chars}자 내외)
- 추가 작성 지시: {section_brief or '(없음)'}
{sub_note}
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
{(bid.rfp_full_text or bid.rfp_summary or '')[:18000]}

# 📦 본 섹션을 위해 큐레이션된 회사 자산·레퍼런스
{context_block or '(이 섹션에 매칭되는 자산이 등록되어 있지 않다. 회사 정보·차별점 위주로 작성하라.)'}

{global_ctx_block}

# 팀장 추가 지시
{team_extra or '(없음)'}

{version_note}

위 정보로 본인의 전문 영역에 충실한 본문을 작성하라.
시스템 프롬프트의 형식 규칙·필수 항목·분량 목표를 모두 지킬 것.
"""

        full_system = specialist.system_prompt + _SLIDE_SPEC_INSTRUCTION
        max_tokens_with_slides = min(8000, max_tokens + 1500)
        raw = chat_anthropic(
            self._model(),
            full_system,
            user_prompt,
            max_tokens=max_tokens_with_slides,
        )
        return _split_body_and_slides(raw)

    def write_section_with_specialist(
        self,
        section_def: Dict,
        bid: BidNotice,
        version: int,
        order: int,
        global_ctx: Optional[Dict] = None,
        chapter_index: int = 0,
        chapters_total: int = 0,
        prev_title: str = "",
        next_title: str = "",
    ) -> ProposalSection:
        # 동적 specialist (display_role 반영)
        specialist = self.make_dynamic_specialist(section_def)

        # 회사 소개·유사 실적·투입 인력은 사람이 직접 채울 영역 → 자리표시자만 생성
        if specialist.key in ("organization", "company"):
            placeholder = (
                f"# {section_def['title']} (사람 작성 예정)\n\n"
                "이 섹션은 사장님(또는 영업/HR 담당)이 직접 작성합니다. "
                "회사 소개·유사 실적·투입 인력의 핵심 정보(인물명, 자격증, 고객사명, 금액)는 "
                "AI가 임의로 만들 수 없으므로 빈 자리만 잡아 둡니다.\n\n"
                "📌 PPT 빌드 시 표지·간지만 삽입되고, 본문은 빈 페이지 1~2장으로 생성됩니다.\n"
            )
            db.log_audit(AuditLogEntry(
                agent_name=f"{self.name}/{specialist.name}",
                model="(skipped)",
                bid_id=bid.bid_id,
                note=f"section={section_def['title']} placeholder v={version}",
            ))
            return ProposalSection(
                title=section_def["title"],
                body=placeholder,
                order=order,
                specialty=specialist.key,
                owner_name=specialist.name,
                slides_json=_placeholder_slides(section_def["title"]),
            )

        global_ctx_block = self._format_global_context(
            global_ctx or {},
            current_idx=chapter_index,
            total=chapters_total,
            prev_title=prev_title,
            next_title=next_title,
        )

        sub_chapters = section_def.get("sub_chapters") or []
        # ── 분할 작성: sub_chapters 있으면 sub마다 LLM 1회씩 호출 ──
        if sub_chapters:
            section_target_pages = int(section_def.get("target_pages") or specialist.default_pages)
            body_parts: List[str] = [f"# {section_def['title']}\n"]
            slides_json: List[Dict] = [{
                "layout": "section_divider",
                "title": section_def["title"],
                "subtitle": specialist.role,
            }]
            for sub in sub_chapters:
                sub_title = sub.get("title") or "(소제목 없음)"
                sub_pages = int(sub.get("target_pages") or 2)
                sub_brief = sub.get("brief") or section_def.get("brief", "")
                try:
                    sub_body, sub_slides = self._llm_section_call(
                        bid=bid,
                        specialist=specialist,
                        section_title=sub_title,
                        section_brief=sub_brief,
                        target_pages=sub_pages,
                        version=version,
                        global_ctx_block=global_ctx_block,
                        is_subchapter=True,
                        parent_title=section_def["title"],
                    )
                except Exception as e:
                    logger.error(
                        f"[{self.name}/{specialist.name}] sub-chapter 실패 ({sub_title}): {e}"
                    )
                    sub_body = f"\n## {sub_title}\n\n(sub-chapter 생성 실패: {e})\n"
                    sub_slides = []
                body_parts.append(f"\n## {sub_title}\n\n{sub_body}\n")
                # sub_slides는 그대로 합치되, sub의 첫 section_divider는 제거(상위 1회만)
                for sl in sub_slides:
                    if sl.get("layout") == "section_divider":
                        continue
                    slides_json.append(sl)
                db.log_audit(AuditLogEntry(
                    agent_name=f"{self.name}/{specialist.name}",
                    model=self._model(),
                    bid_id=bid.bid_id,
                    note=(
                        f"section={section_def['title']} > {sub_title} "
                        f"pages={sub_pages} slides={len(sub_slides)} v={version}"
                    ),
                ))
            body = "\n".join(body_parts)
            logger.info(
                f"[{self.name}/{specialist.name}] '{section_def['title']}' "
                f"sub {len(sub_chapters)}개 작성 완료 (총 슬라이드 {len(slides_json)}장, "
                f"목표 {section_target_pages}p)"
            )
            return ProposalSection(
                title=section_def["title"],
                body=body,
                order=order,
                specialty=specialist.key,
                owner_name=specialist.name,
                slides_json=slides_json,
            )

        # ── 단일 호출 (sub 없음) ──
        target_pages = int(section_def.get("target_pages") or specialist.default_pages)
        body, slides_json = self._llm_section_call(
            bid=bid,
            specialist=specialist,
            section_title=section_def["title"],
            section_brief=section_def.get("brief") or "",
            target_pages=target_pages,
            version=version,
            global_ctx_block=global_ctx_block,
        )
        if not slides_json:
            logger.warning(
                f"[{self.name}/{specialist.name}] slides_json 비어있음 — placeholder 1장으로 대체"
            )
            slides_json = [{
                "layout": "section_divider",
                "title": section_def["title"],
                "subtitle": specialist.role,
            }]
        db.log_audit(AuditLogEntry(
            agent_name=f"{self.name}/{specialist.name}",
            model=self._model(),
            bid_id=bid.bid_id,
            note=f"section={section_def['title']} pages={target_pages} slides={len(slides_json)} v={version}",
        ))
        return ProposalSection(
            title=section_def["title"],
            body=body,
            order=order,
            specialty=specialist.key,
            owner_name=specialist.name,
            slides_json=slides_json,
        )

    def draft_proposal(self, bid: BidNotice, version: int = 1, progress_cb=None) -> ProposalDraft:
        # 1) 저장된 RFP 목차 트리(+global_context) 우선 사용.
        #    - RFP 추출기는 항상 global_context.vision 또는 key_keywords를 함께 채운다.
        #    - 또한 추출된 챕터에는 display_role 필드가 있다 (baseline 8명 구조에는 없음).
        #    이 두 시그널 중 하나라도 있으면 RFP 트리 모드로 인식.
        saved_toc = db.get_proposal_toc(bid.bid_id)
        global_ctx = db.get_proposal_toc_global_context(bid.bid_id) or {}
        is_rfp_tree = bool(saved_toc) and (
            bool((global_ctx.get("vision") or "").strip())
            or bool(global_ctx.get("key_keywords"))
            or any((s.get("display_role") or "").strip() for s in saved_toc)
            or any(s.get("sub_chapters") for s in saved_toc)
        )
        toc: List[Dict]
        if is_rfp_tree:
            toc = saved_toc
            logger.info(
                f"[{self.name}] 저장된 RFP 목차 트리 사용 ({len(toc)}챕터, "
                f"sub {sum(len(s.get('sub_chapters', [])) for s in toc)}개, "
                f"vision={'O' if global_ctx.get('vision') else 'X'})"
            )
        else:
            toc = self.plan_toc(bid)
            global_ctx = global_ctx or {}

        # 총 LLM sub-call 수 가드 (sub_chapters 합 > 40 이면 큰 챕터의 sub를 병합)
        MAX_SUB_CALLS = 40
        total_sub = sum(len(s.get("sub_chapters") or []) for s in toc)
        if total_sub > MAX_SUB_CALLS:
            logger.warning(
                f"[{self.name}] sub-call 합계 {total_sub} > {MAX_SUB_CALLS} — 큰 챕터의 sub 병합"
            )
            for s in toc:
                subs = s.get("sub_chapters") or []
                if len(subs) > 3:
                    # 3개 묶음으로 코어레스
                    merged: List[dict] = []
                    chunk = (len(subs) + 2) // 3
                    for i in range(0, len(subs), chunk):
                        group = subs[i:i + chunk]
                        merged.append({
                            "title": group[0].get("title", "") + (
                                f" 외 {len(group) - 1}개" if len(group) > 1 else ""
                            ),
                            "target_pages": sum(int(g.get("target_pages") or 1) for g in group),
                            "brief": " / ".join(
                                (g.get("brief") or "").strip()
                                for g in group if g.get("brief")
                            )[:600],
                        })
                    s["sub_chapters"] = merged
            new_total = sum(len(s.get("sub_chapters") or []) for s in toc)
            logger.info(f"[{self.name}] sub-call 병합 후 합계: {new_total}")

        total = len(toc)
        logger.info(
            f"[{self.name}] 제안서 작성 시작 ({bid.bid_id} v{version}, {total}챕터)"
        )
        if progress_cb:
            progress_cb(0, total, f"목차 {total}챕터 확정 — 전문가 동시 작성 시작")

        sections: List[ProposalSection] = []
        done = 0
        # sub_chapters 분할 작성은 챕터 1건당 LLM 여러 번 호출되므로 병렬도는 그대로 3 유지
        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = {
                ex.submit(
                    self.write_section_with_specialist,
                    s, bid, version, idx,
                    global_ctx,  # global_ctx
                    idx,         # chapter_index
                    total,       # chapters_total
                    toc[idx - 1]["title"] if idx > 0 else "",
                    toc[idx + 1]["title"] if idx + 1 < total else "",
                ): (idx, s)
                for idx, s in enumerate(toc)
            }
            for fut in as_completed(futures):
                idx, s = futures[fut]
                try:
                    sec = fut.result()
                    sections.append(sec)
                    db.log_activity(
                        "park", "info", f"섹션 완료: {s['title']}", bid_id=bid.bid_id,
                    )
                    logger.debug(f"[{self.name}] 섹션 완료: {s['title']}")
                except Exception as e:
                    logger.error(f"[{self.name}] 섹션 실패 ({s['title']}): {e}")
                    sections.append(ProposalSection(
                        title=s["title"],
                        body=f"(섹션 생성 실패: {e})",
                        order=idx,
                    ))
                done += 1
                if progress_cb:
                    progress_cb(done, total, f"섹션 완료: {s['title']}")

        sections.sort(key=lambda x: x.order)

        draft = ProposalDraft(bid_id=bid.bid_id, version=version, sections=sections)
        # DOCX는 더 이상 제출용으로 사용하지 않으나, 참고용 백업 산출물로만 남긴다.
        try:
            docx_path = render_proposal_docx(draft, bid.title)
            draft.docx_path = str(docx_path)
        except Exception as e:
            logger.warning(f"[{self.name}] DOCX 백업 생성 실패(무시): {e}")
        db.save_proposal(draft)

        # ★ 게이트2 산출물 = 기획 PPT (스토리보드)
        try:
            from tools.storyboard_generator import render_storyboard
            if progress_cb:
                progress_cb(total, total, "스토리보드 PPT 생성 중…")
            sb_path = render_storyboard(bid, draft)
            db.update_proposal_storyboard(bid.bid_id, version, str(sb_path))
            logger.info(f"[{self.name}] 스토리보드 생성: {sb_path}")
        except Exception as e:
            logger.error(f"[{self.name}] 스토리보드 생성 실패(무시하고 계속): {e}")

        db.set_bid_status(bid.bid_id, BidStatus.DRAFT_DONE)
        logger.info(f"[{self.name}] 제안서 초안 완료 (sections={len(sections)})")
        return draft

    def run(self, bid_id: Optional[str] = None, progress_cb=None) -> List[ProposalDraft]:
        if bid_id:
            bid = db.get_bid(bid_id)
            if not bid:
                return []
            draft = self.draft_proposal(bid, progress_cb=progress_cb)
            return [draft]
        bids = db.list_bids(status=BidStatus.APPROVED)
        total = len(bids)
        logger.info(f"[{self.name}] 작성 대상 {total}건")
        if progress_cb:
            progress_cb(0, total, f"작성 대상 {total}건")
        results = []
        for idx, b in enumerate(bids, start=1):
            results.append(self.draft_proposal(b))
            if progress_cb:
                progress_cb(idx, total, f"{b.title[:30]} 초안 완료")
        return results


# 후방 호환
ParkProposer = ParkTeam
