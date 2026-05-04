"""
DiscoveryAgent — 공고 발견 + RFP 파싱 + RAG 인덱싱.

v1 KimDetective의 후속. 변경점:
  - BaseAgent 상속, 6-에이전트 체계의 "discovery" 슬롯
  - 신규 공고에 첨부된 RFP PDF를 즉시 파싱(rfp_parser → rag.pdf_processor)
  - 파싱 결과를 ChromaDB `rfp_documents` 컬렉션에 청크 단위로 인덱싱
  - 인덱싱은 best-effort: 의존성/키 누락 시 수집 자체는 계속 진행

호출 예:
    bids = DiscoveryAgent().run()
"""

from __future__ import annotations

from typing import List, Optional

from loguru import logger

from agents.base_agent import BaseAgent, ProgressCallback
from config.settings import (
    MODELS,
    VECTOR,
    get_collection_keywords,
    get_extra_instructions,
)
from schemas.models import BidNotice
from tools import agency_crawler, db, g2b_api
from tools.rfp_parser import parse_rfp_full


class DiscoveryAgent(BaseAgent):
    agent_name = "discovery"

    def __init__(
        self,
        progress_cb: Optional[ProgressCallback] = None,
        *,
        index_rfps: bool = True,
    ) -> None:
        super().__init__(progress_cb)
        self.model = MODELS.discovery
        self._index_rfps = index_rfps

    def run(self) -> List[BidNotice]:
        keywords = get_collection_keywords()
        instructions = get_extra_instructions("discovery")

        self.progress(f"수집 시작 (model={self.model}, 키워드={keywords})")
        if instructions:
            logger.info(f"[{self.agent_name}] 사용자 지시: {instructions[:80]}")

        collected = self._collect(keywords)
        all_unique, new_bids = self._dedup(collected)

        if self._index_rfps and new_bids:
            self._enrich_and_index(new_bids)

        # RFP 본문이 채워진 상태로 신규 공고를 DB에 적재
        for bid in new_bids:
            db.upsert_bid(bid)

        self.progress(
            f"수집 완료: 총 {len(all_unique)}건 (신규 {len(new_bids)}건)"
        )
        return all_unique

    # ------------------------------------------------------------------
    # 내부 단계
    # ------------------------------------------------------------------

    def _collect(self, keywords: List[str]) -> List[BidNotice]:
        collected: List[BidNotice] = []

        for kw in keywords:
            try:
                bids = g2b_api.search_bids(kw)
                self.progress(f"G2B '{kw}' → {len(bids)}건")
                collected.extend(bids)
            except Exception as e:
                logger.warning(f"[{self.agent_name}] G2B 조회 실패({kw}): {e}")

        try:
            agency_bids = agency_crawler.crawl_all_agencies()
            self.progress(f"지자체 크롤링 → {len(agency_bids)}건")
            collected.extend(agency_bids)
        except Exception as e:
            logger.warning(f"[{self.agent_name}] 지자체 크롤링 실패: {e}")

        return collected

    def _dedup(
        self, collected: List[BidNotice]
    ) -> tuple[List[BidNotice], List[BidNotice]]:
        """현재 배치 내 중복 제거 + DB 기준 신규/기존 분리."""
        seen: set[str] = set()
        all_unique: List[BidNotice] = []
        new_bids: List[BidNotice] = []
        for b in collected:
            if b.bid_id in seen:
                continue
            seen.add(b.bid_id)
            all_unique.append(b)
            if db.get_bid(b.bid_id) is None:
                new_bids.append(b)
        return all_unique, new_bids

    def _enrich_and_index(self, bids: List[BidNotice]) -> None:
        """신규 공고의 RFP를 파싱해 BidNotice를 채우고 ChromaDB에 인덱싱."""
        try:
            from rag.chunker import chunk_pdf_doc
            from rag.embeddings import embed_documents
            from rag.vector_store import VectorStore
        except ImportError as e:
            logger.warning(
                f"[{self.agent_name}] RAG 의존성 누락 — RFP 인덱싱 스킵: {e}"
            )
            return

        store: Optional[VectorStore] = None  # 첫 PDF 발견 시점에 lazy 초기화

        for bid in bids:
            if not bid.rfp_pdf_path:
                continue

            doc = parse_rfp_full(bid.rfp_pdf_path)
            if not doc:
                continue

            bid.rfp_full_text = doc.get("raw_text", "") or None

            # 청크 doc_id를 bid_id로 통일하기 위해 filename을 갈아끼움
            doc_for_chunk = dict(doc)
            doc_for_chunk["filename"] = f"{bid.bid_id}.pdf"
            chunks = chunk_pdf_doc(doc_for_chunk)
            if not chunks:
                continue

            for c in chunks:
                c["metadata"]["bid_id"] = bid.bid_id
                c["metadata"]["agency"] = bid.agency

            try:
                if store is None:
                    store = VectorStore(collection_name=VECTOR.collection_rfp)
                texts = [c["text"] for c in chunks]
                embeddings = embed_documents(
                    texts, batch_size=VECTOR.embedding_batch_size
                )
                added = store.add_chunks(chunks, embeddings)
                self.progress(f"RFP 인덱싱: {bid.bid_id} → {added}청크")
            except Exception as e:
                logger.warning(
                    f"[{self.agent_name}] RFP 인덱싱 실패({bid.bid_id}): {e}"
                )
