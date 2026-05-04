"""
ChromaDB 벡터 스토어 래퍼.
- reference_proposals 컬렉션 관리
- 청크 추가 / 유사도 검색 / 통계 조회
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

try:
    import chromadb
    from chromadb.config import Settings
    _CHROMA_AVAILABLE = True
except ImportError:
    _CHROMA_AVAILABLE = False

_DEFAULT_COLLECTION = "reference_proposals"


def _get_client(persist_dir: str | Path | None = None) -> Any:
    if not _CHROMA_AVAILABLE:
        raise ImportError("chromadb 미설치. pip install chromadb")
    if persist_dir is None:
        from config.settings import CHROMA_DB_PATH
        persist_dir = CHROMA_DB_PATH
    persist_dir = Path(persist_dir)
    persist_dir.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(persist_dir))


class VectorStore:
    """ChromaDB 컬렉션 인터페이스.

    기본 컬렉션은 학습 코퍼스(`reference_proposals`)지만, DiscoveryAgent의
    RFP 인덱싱처럼 다른 컬렉션이 필요한 경우 `collection_name`을 지정한다
    (예: VECTOR.collection_rfp = "rfp_documents").
    """

    def __init__(
        self,
        persist_dir: str | Path | None = None,
        collection_name: str = _DEFAULT_COLLECTION,
    ):
        self._client = _get_client(persist_dir)
        self._collection_name = collection_name
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(
            f"VectorStore 초기화: {collection_name} "
            f"(현재 {self._collection.count()}건)"
        )

    # ------------------------------------------------------------------
    # 쓰기
    # ------------------------------------------------------------------

    def add_chunks(
        self,
        chunks: list[dict[str, Any]],
        embeddings: list[list[float]],
    ) -> int:
        """청크와 임베딩을 컬렉션에 추가. 이미 존재하는 ID는 upsert."""
        if not chunks:
            return 0

        valid = [
            (c, e) for c, e in zip(chunks, embeddings) if e
        ]
        if not valid:
            logger.warning("유효한 임베딩이 없습니다.")
            return 0

        ids = [c["id"] for c, _ in valid]
        docs = [c["text"] for c, _ in valid]
        embs = [e for _, e in valid]
        metas = []
        for c, _ in valid:
            # ChromaDB는 None 값을 허용하지 않으므로 빈 문자열로 변환
            meta = {
                k: (v if v is not None else "")
                for k, v in c.get("metadata", {}).items()
            }
            # int/float 값은 그대로 유지
            metas.append(meta)

        BATCH = 500
        added = 0
        for i in range(0, len(ids), BATCH):
            self._collection.upsert(
                ids=ids[i : i + BATCH],
                documents=docs[i : i + BATCH],
                embeddings=embs[i : i + BATCH],
                metadatas=metas[i : i + BATCH],
            )
            added += len(ids[i : i + BATCH])

        return added

    def delete_by_doc_id(self, doc_id: str) -> None:
        """특정 문서의 모든 청크 삭제."""
        self._collection.delete(where={"doc_id": doc_id})
        logger.info(f"문서 삭제: {doc_id}")

    def clear(self) -> None:
        """컬렉션 전체 초기화."""
        self._client.delete_collection(self._collection_name)
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(f"벡터 스토어 초기화 완료: {self._collection_name}")

    # ------------------------------------------------------------------
    # 읽기
    # ------------------------------------------------------------------

    def query(
        self,
        query_embedding: list[float],
        n_results: int = 5,
        where: dict | None = None,
    ) -> list[dict[str, Any]]:
        """벡터 유사도 검색. 결과 리스트 반환."""
        kwargs: dict[str, Any] = {
            "query_embeddings": [query_embedding],
            "n_results": min(n_results, max(self._collection.count(), 1)),
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        results = self._collection.query(**kwargs)

        output: list[dict[str, Any]] = []
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        for doc, meta, dist in zip(docs, metas, distances):
            output.append({
                "text": doc,
                "metadata": meta,
                "score": round(1 - dist, 4),  # cosine distance → similarity
            })

        return output

    def count(self) -> int:
        return self._collection.count()

    def get_stats(self) -> dict[str, Any]:
        """컬렉션 통계 (총 청크 수, 문서별 청크 수)."""
        total = self._collection.count()
        if total == 0:
            return {"total_chunks": 0, "documents": {}}

        all_items = self._collection.get(include=["metadatas"])
        doc_counts: dict[str, int] = {}
        layout_counts: dict[str, int] = {}

        for meta in all_items.get("metadatas", []):
            if meta:
                doc_id = meta.get("source_file", "unknown")
                doc_counts[doc_id] = doc_counts.get(doc_id, 0) + 1
                layout = meta.get("layout_mode", "")
                if layout:
                    layout_counts[layout] = layout_counts.get(layout, 0) + 1

        return {
            "total_chunks": total,
            "documents": doc_counts,
            "layout_distribution": layout_counts,
        }
