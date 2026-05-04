# chroma_db/

**시스템이 자동으로 관리하는 폴더입니다. 직접 파일을 넣거나 수정하지 마세요.**

## 용도

- ChromaDB 벡터 데이터베이스 파일이 저장됩니다.
- 제안서 텍스트를 임베딩(embedding)한 벡터 인덱스가 이 폴더에 유지됩니다.
- RAG 검색 시 이 DB를 조회하여 유사 제안서 내용을 검색합니다.

## 자동 생성 파일 예시

```
chroma_db/
├── chroma.sqlite3
└── [uuid]/
    ├── data_level0.bin
    ├── header.bin
    ├── index_metadata.pickle
    └── length.bin
```

## 초기화 방법

벡터 DB를 초기화해야 할 경우 이 폴더의 내용을 모두 삭제한 후 임베딩 파이프라인을 다시 실행하세요.

```bash
rm -rf proposal_ai/data/chroma_db/*
python proposal_ai/tools/embed_proposals.py
```

## 주의사항

- 이 폴더 전체는 `.gitignore`에 의해 git에서 추적되지 않습니다.
- DB 파일이 손상된 경우 폴더를 비우고 재생성하면 복구됩니다.
