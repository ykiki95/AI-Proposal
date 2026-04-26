"""
지자체 웹사이트 크롤러.
- 정적 페이지: requests + BeautifulSoup
- 동적 페이지: playwright (선택 설치, 미설치 시 자동 skip)
- agency_selectors.yaml에서 셀렉터 로딩
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import List

import requests
import yaml
from bs4 import BeautifulSoup
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from config.settings import ROOT_DIR
from schemas.models import BidNotice

SELECTORS_FILE = ROOT_DIR / "config" / "agency_selectors.yaml"


def load_config() -> dict:
    with SELECTORS_FILE.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def keyword_match(title: str, cfg: dict) -> bool:
    inc = cfg.get("keywords", {}).get("include", [])
    exc = cfg.get("keywords", {}).get("exclude", [])
    if not any(kw in title for kw in inc):
        return False
    if any(kw in title for kw in exc):
        return False
    return True


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
def _fetch_static(url: str) -> str:
    r = requests.get(
        url,
        timeout=8,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ProposalAI/1.0)"},
    )
    r.raise_for_status()
    return r.text


def crawl_agency(agency_cfg: dict, cfg: dict) -> List[BidNotice]:
    """단일 지자체 페이지에서 공고 목록 수집."""
    name = agency_cfg["name"]
    url = agency_cfg["list_url"]
    selectors = agency_cfg.get("selectors", {})

    if agency_cfg.get("type") == "dynamic":
        try:
            html = _fetch_dynamic(url)
        except Exception as e:
            logger.warning(f"[{name}] dynamic 크롤링 실패({e}), 정적 모드로 fallback.")
            try:
                html = _fetch_static(url)
            except Exception as e2:
                logger.warning(f"[{name}] 정적 fallback도 실패({e2}). skip.")
                return []
    else:
        try:
            html = _fetch_static(url)
        except Exception as e:
            logger.warning(f"[{name}] 정적 크롤링 실패({e}). skip.")
            return []

    soup = BeautifulSoup(html, "html.parser")
    items = soup.select(selectors.get("item", "")) if selectors.get("item") else []

    out: List[BidNotice] = []
    for idx, it in enumerate(items[:30]):
        try:
            title_el = it.select_one(selectors.get("title", "")) if selectors.get("title") else None
            link_el = it.select_one(selectors.get("link", "")) if selectors.get("link") else None
            title = title_el.get_text(strip=True) if title_el else ""
            link = link_el.get("href") if link_el else ""
            if not title:
                continue
            if not keyword_match(title, cfg):
                continue
            out.append(BidNotice(
                bid_id=f"{agency_cfg['code']}-{datetime.now().strftime('%Y%m%d')}-{idx:03d}",
                source="지자체",
                agency=name,
                title=title,
                budget_krw=None,
                duration_months=None,
                qualifications=[],
                deadline=datetime.now() + timedelta(days=14),
                rfp_summary=title,
                rfp_url=link or url,
            ))
        except Exception as e:
            logger.debug(f"[{name}] 항목 파싱 실패: {e}")
    logger.info(f"[{name}] 키워드 매칭 공고 {len(out)}건 수집")
    return out


def _fetch_dynamic(url: str) -> str:
    """Playwright 기반 동적 렌더링. 미설치 시 ImportError."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "playwright 미설치. `pip install playwright && playwright install chromium`"
        ) from e

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        html = page.content()
        browser.close()
        return html


def crawl_all_agencies() -> List[BidNotice]:
    """설정된 모든 지자체 페이지를 순회 크롤링."""
    cfg = load_config()
    out: List[BidNotice] = []
    for agency in cfg.get("agencies", []):
        out.extend(crawl_agency(agency, cfg))
    return out
