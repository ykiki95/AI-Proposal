# processed/

**시스템이 자동으로 관리하는 폴더입니다. 직접 파일을 넣거나 수정하지 마세요.**

## 용도

- `winning_proposals/`와 `test_rfps/`의 PDF를 파싱·정제한 텍스트 데이터가 저장됩니다.
- 청크(chunk) 단위로 분할된 텍스트, 메타데이터 JSON 파일 등이 포함됩니다.

## 자동 생성 파일 예시

```
processed/
├── winning_proposals/
│   ├── 2024_스마트팩토리_ABC사_수주.json
│   └── ...
└── test_rfps/
    ├── 2024_스마트시티_RFP_테스트.json
    └── ...
```

## 주의사항

- 이 폴더 전체는 `.gitignore`에 의해 git에서 추적되지 않습니다.
- 원본 PDF를 수정한 경우 이 폴더를 삭제하고 전처리를 다시 실행하세요.
