# data/

AI 제안서 시스템에서 사용하는 모든 데이터 파일의 루트 폴더입니다.

## 폴더 구조

| 폴더 | 용도 |
|------|------|
| `winning_proposals/` | 수주 성공 제안서 PDF 파일 |
| `winning_proposals_pptx/` | 수주 성공 제안서 PPT 원본 (디자인 패턴 추출용) |
| `test_rfps/` | 테스트용 RFP(제안요청서) PDF 파일 |
| `processed/` | 전처리 완료된 텍스트 데이터 (자동 생성) |
| `chroma_db/` | 벡터 데이터베이스 저장소 (자동 생성) |

## 주의사항

- `processed/`와 `chroma_db/`는 시스템이 자동으로 생성·관리합니다. 직접 수정하지 마세요.
- 실제 데이터 파일(PDF, PPTX)은 `.gitignore`에 의해 git에서 추적되지 않습니다.
- 각 하위 폴더의 `README.md`를 참고하여 파일을 올바른 위치에 넣어주세요.
