"""
나라장터(G2B) OpenAPI 클라이언트.
- 키 미설정 시 샘플 공고 데이터를 반환 (개발/데모 모드)
- 1초 1회 rate limit 준수
- tenacity 지수 백오프 재시도
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import List, Optional

import requests
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from config.settings import KEYS
from schemas.models import BidNotice

# 나라장터 표준 입찰공고 검색 OpenAPI 엔드포인트
G2B_BASE = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService"

# 입찰 분야별 메서드 (공공데이터포털 활용신청에서 함께 신청 가능)
G2B_METHODS = [
    ("용역", "getBidPblancListInfoServc"),
    # 공사는 우리 회사 사업영역이 아니므로 제외 (사장 지시 2026-04)
    # 필요 시 추가: ("물품", "getBidPblancListInfoThng"), ("외자", "getBidPblancListInfoFrgcpt")
]


_last_call_at = 0.0


def _rate_limit() -> None:
    """1초당 1회 호출 제한."""
    global _last_call_at
    delta = time.time() - _last_call_at
    if delta < 1.0:
        time.sleep(1.0 - delta)
    _last_call_at = time.time()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def _http_get(url: str, params: dict) -> dict:
    _rate_limit()
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def search_bids(keyword: str, page: int = 1, num_rows: int = 50) -> List[BidNotice]:
    """키워드로 G2B 공고 조회. 용역(기술용역 포함)만 호출 — 공사는 제외.
    키 없거나 API 실패 시 샘플 데이터 반환."""
    if not KEYS.has_g2b:
        logger.info("G2B 키 미설정 - 샘플 데이터를 반환합니다.")
        try:
            from tools import db
            db.log_activity("kim", "info",
                "G2B 키가 등록되지 않아 샘플 공고를 사용합니다 (Replit Secrets에 G2B_SERVICE_KEY 설정 필요).")
        except Exception:
            pass
        return _sample_bids(keyword)

    # 최근 7일치 공고 검색 (G2B inqryBgnDt/inqryEndDt 필수)
    from datetime import datetime, timedelta
    end_dt = datetime.now()
    bgn_dt = end_dt - timedelta(days=7)

    base_params = {
        "serviceKey": KEYS.g2b_service_key,
        "pageNo": page,
        "numOfRows": num_rows,
        "type": "json",
        "bidNtceNm": keyword,
        "inqryDiv": 1,
        "inqryBgnDt": bgn_dt.strftime("%Y%m%d") + "0000",
        "inqryEndDt": end_dt.strftime("%Y%m%d") + "2359",
    }

    out: List[BidNotice] = []
    failure_msgs: List[str] = []

    for label, method in G2B_METHODS:
        try:
            data = _http_get(f"{G2B_BASE}/{method}", base_params)
        except Exception as e:
            msg = f"{label}({method}): {str(e)[:120]}"
            logger.warning(f"G2B 호출 실패 - {msg}")
            failure_msgs.append(msg)
            continue

        items = (
            data.get("response", {})
            .get("body", {})
            .get("items", [])
        )
        if isinstance(items, dict):
            items = items.get("item", [])
        if not isinstance(items, list):
            items = []

        for it in items:
            try:
                out.append(_parse_g2b_item(it))
            except Exception as e:
                logger.debug(f"G2B 항목 파싱 실패: {e}")

        logger.info(f"[G2B {label}] '{keyword}' → {len(items)}건")

    # 모두 실패한 경우만 샘플로 fallback (활성화 미완료 등)
    if not out and failure_msgs:
        try:
            from tools import db
            db.log_activity("kim", "error",
                f"G2B 호출 전건 실패('{keyword}'): {' / '.join(failure_msgs)[:200]}. 샘플로 대체.")
        except Exception:
            pass
        return _sample_bids(keyword)

    return out


def _parse_g2b_item(item: dict) -> BidNotice:
    deadline_str = item.get("bidClseDt") or item.get("opengDt") or ""
    try:
        deadline = datetime.strptime(deadline_str[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        deadline = datetime.now() + timedelta(days=14)

    budget_raw = item.get("presmptPrce") or item.get("asignBdgtAmt") or 0
    try:
        budget = int(float(str(budget_raw).replace(",", ""))) if budget_raw else None
    except Exception:
        budget = None

    return BidNotice(
        bid_id=str(item.get("bidNtceNo", "")) or f"G2B-{int(time.time())}",
        source="G2B",
        agency=item.get("ntceInsttNm", "미상"),
        title=item.get("bidNtceNm", "제목없음"),
        budget_krw=budget,
        duration_months=None,
        qualifications=[item.get("prtcptPsblRgnNm", "")] if item.get("prtcptPsblRgnNm") else [],
        deadline=deadline,
        rfp_summary=item.get("bidNtceDtlUrl", ""),
        rfp_url=item.get("bidNtceDtlUrl", ""),
        rfp_full_text=None,
    )


def _sample_bids(keyword: str) -> List[BidNotice]:
    """G2B 키 미설정 시 사용하는 가상 공고."""
    now = datetime.now()
    return [
        BidNotice(
            bid_id=f"SAMPLE-{now.strftime('%Y%m%d')}-001",
            source="G2B",
            agency="서울특별시",
            title=f"[{keyword}] 시민 참여형 통합 포털 신규 구축 사업",
            budget_krw=180_000_000,
            duration_months=6,
            qualifications=["소프트웨어사업자 신고", "유사 실적 1건 이상"],
            deadline=now + timedelta(days=21),
            rfp_summary="시민이 직접 정책 제안에 참여할 수 있는 통합 포털을 신규 구축. "
                        "React 기반 반응형 웹과 모바일 앱, 관리자 시스템 포함.",
            rfp_url="https://www.g2b.go.kr/sample/001",
            rfp_full_text=(
                "1. 사업명: 시민 참여형 통합 포털 신규 구축\n"
                "2. 사업기간: 계약일로부터 6개월\n"
                "3. 예산: 180,000,000원 (부가세 포함)\n"
                "4. 주요 요구사항:\n"
                "  - React 18 기반 SPA\n"
                "  - Node.js + PostgreSQL 백엔드\n"
                "  - 회원가입/SSO/관리자 권한관리\n"
                "  - 정책 제안/투표/댓글 기능\n"
                "  - 모바일 반응형 + PWA\n"
                "  - WCAG 2.1 AA 웹 접근성 준수\n"
                "5. 제출서류: 제안서, PT자료, 사업수행계획서\n"
            ),
        ),
        BidNotice(
            bid_id=f"SAMPLE-{now.strftime('%Y%m%d')}-002",
            source="G2B",
            agency="경기도청",
            title=f"[{keyword}] 도청 대표 홈페이지 전면 리뉴얼",
            budget_krw=320_000_000,
            duration_months=8,
            qualifications=["대기업 참여제한", "지역업체 우대"],
            deadline=now + timedelta(days=10),
            rfp_summary="기존 홈페이지 노후화에 따른 전면 리뉴얼. UI/UX 개선과 다국어 지원 포함.",
            rfp_url="https://www.g2b.go.kr/sample/002",
            rfp_full_text=(
                "1. 사업명: 경기도청 대표 홈페이지 전면 리뉴얼\n"
                "2. 사업기간: 8개월\n"
                "3. 예산: 320,000,000원\n"
                "4. 핵심 요구사항:\n"
                "  - Next.js + Headless CMS\n"
                "  - 4개국어 지원(한/영/중/일)\n"
                "  - 통합 검색 (Elasticsearch)\n"
                "  - 모바일 반응형\n"
                "  - 행정안전부 웹표준/접근성 준수\n"
                "  - 클라우드(NCP/AWS) 인프라 구성\n"
                "5. 평가: 기술 80 / 가격 20\n"
            ),
        ),
        BidNotice(
            bid_id=f"SAMPLE-{now.strftime('%Y%m%d')}-003",
            source="G2B",
            agency="한국관광공사",
            title=f"[{keyword}] AI 기반 관광 추천 플랫폼 구축",
            budget_krw=750_000_000,
            duration_months=10,
            qualifications=["대기업 참여 가능", "AI 관련 실적 5억 이상"],
            deadline=now + timedelta(days=4),
            rfp_summary="개인 맞춤형 관광 추천 AI 플랫폼 신규 구축.",
            rfp_url="https://www.g2b.go.kr/sample/003",
            rfp_full_text=(
                "1. 사업명: AI 기반 관광 추천 플랫폼\n"
                "2. 예산: 750,000,000원\n"
                "3. 사업기간: 10개월\n"
                "4. 요구사항:\n"
                "  - LLM 기반 자연어 질의\n"
                "  - 추천 엔진 (Python, PyTorch)\n"
                "  - 5개국어\n"
                "  - 모바일 앱(iOS/Android)\n"
                "5. 자격: 단일 사업 5억 이상 실적 1건\n"
            ),
        ),
    ]
