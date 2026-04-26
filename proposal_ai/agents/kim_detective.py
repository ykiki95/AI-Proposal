"""
김탐정 - 정보 수집 에이전트.
G2B + 지자체 페이지에서 공고를 수집하고 중복 제거 + 키워드 1차 필터.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from loguru import logger

from config.settings import MODELS, get_collection_keywords, get_extra_instructions
from schemas.models import BidNotice
from tools import agency_crawler, db, g2b_api


class KimDetective:
    name = "김탐정"
    role = "공공/민간 제안 공고 수집"
    model = MODELS.kim

    def run(
        self,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
    ) -> List[BidNotice]:
        keywords = get_collection_keywords()
        instructions = get_extra_instructions("kim")
        logger.info(
            f"[{self.name}] 수집 시작 (model={self.model}, 키워드={keywords})"
        )
        if instructions:
            logger.info(f"[{self.name}] 사장님 지시: {instructions[:80]}")

        collected: List[BidNotice] = []
        total_steps = len(keywords) + 1  # 키워드별 G2B + 지자체 크롤링 1단계
        done = 0

        if progress_cb:
            progress_cb(done, total_steps, "수집 준비 중...")

        # 1. G2B - 키워드 다중 조회 (사용자가 대시보드에서 변경 가능)
        for kw in keywords:
            try:
                bids = g2b_api.search_bids(kw)
                logger.info(f"[{self.name}] G2B '{kw}' → {len(bids)}건")
                collected.extend(bids)
            except Exception as e:
                logger.warning(f"[{self.name}] G2B 조회 실패({kw}): {e}")
            done += 1
            if progress_cb:
                progress_cb(done, total_steps, f"G2B 키워드 '{kw}' 완료 ({len(collected)}건 누적)")

        # 2. 지자체 크롤링
        try:
            agency_bids = agency_crawler.crawl_all_agencies()
            collected.extend(agency_bids)
        except Exception as e:
            logger.warning(f"[{self.name}] 지자체 크롤링 실패: {e}")
        done += 1
        if progress_cb:
            progress_cb(done, total_steps, f"지자체 크롤링 완료 ({len(collected)}건 누적)")

        # 3. 중복 제거 + DB 저장
        new_count = 0
        unique: List[BidNotice] = []
        seen = set()
        for b in collected:
            if b.bid_id in seen:
                continue
            seen.add(b.bid_id)
            if db.upsert_bid(b):
                new_count += 1
            unique.append(b)

        logger.info(
            f"[{self.name}] 수집 완료: 총 {len(unique)}건 (신규 {new_count}건)"
        )
        return unique
