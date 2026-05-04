"""Bedrock 모드 동작 검증 스크립트 (Haiku / Sonnet 2종).

목적:
  1. .env의 USE_BEDROCK + AWS region 분기가 실제 LLM 호출까지 도달하는지.
  2. 매핑된 inference profile이 해당 리전에 활성화돼 있는지 (boto3 list_inference_profiles).
  3. Haiku/Sonnet 두 모델 모두 짧은 호출 1회씩 — 진짜 통과 확인.

설계 메모:
  - 2026-05 현재 Bedrock에는 Claude 4.7/4.6 cross-region profile 미배포.
  - 도쿄 리전 + 4.5 시리즈로 다운그레이드 매핑 (.env BEDROCK_MODEL_* 환경변수).
  - settings.py의 MODELS는 Anthropic 직결 모델명(opus-4-7/sonnet-4-6) 그대로 유지.
    Opus는 Sonnet 4.5로 매핑되므로 Sonnet 검증이 곧 Opus 경로 검증.

비용:
  - Haiku  ~$0.0005 (한국어 1+1, max_tokens=50)
  - Sonnet ~$0.003  (100자 응답, max_tokens=200)
  - 합계   ~$0.005 미만

사용:
  python scripts/test_bedrock.py
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.settings import KEYS, MODELS, SYS
from tools.llm_client import _BEDROCK_MODEL_MAP, chat, get_usage_summary, reset_usage


_FAILURE_HINT = """
예상 원인 — model ID 불일치 가능성:
  매핑된 Bedrock ID: {bedrock_id}
  현재 리전 ({region})에서 이 inference profile이 활성화되지 않았을 수 있습니다.

  .env에 아래 한 줄로 prefix를 바꿔 즉시 시도하세요:
    BEDROCK_MODEL_{slot}=jp.anthropic.claude-...-v1:0     # Tokyo cross-region
    BEDROCK_MODEL_{slot}=apac.anthropic.claude-...-v1:0   # APAC 광역
    BEDROCK_MODEL_{slot}=us.anthropic.claude-...-v1:0     # US cross-region

  또는 AWS_REGION을 변경 (.env에 AWS_REGION=us-west-2 등).

  AWS Console → Bedrock → Cross-region inference 페이지에서 사용 가능한
  정확한 inference profile ID를 복사해 .env에 붙여넣는 것이 가장 확실합니다.
"""


def check_profile_active(target_id: str) -> tuple[bool, str]:
    """boto3 list_inference_profiles로 해당 ID가 현재 리전에 등록돼 있는지 검사."""
    try:
        import boto3
    except ImportError:
        return (False, "boto3 미설치 — anthropic[bedrock] extras 확인")

    region = KEYS.aws_region
    b = boto3.client(
        "bedrock",
        region_name=region,
        aws_access_key_id=KEYS.aws_access_key_id,
        aws_secret_access_key=KEYS.aws_secret_access_key,
    )
    seen: set[str] = set()
    for t in ("SYSTEM_DEFINED", "APPLICATION"):
        try:
            resp = b.list_inference_profiles(maxResults=100, typeEquals=t)
            for p in resp.get("inferenceProfileSummaries", []):
                seen.add(p["inferenceProfileId"])
        except Exception as e:
            return (False, f"list_inference_profiles 오류 ({t}): {e}")
    if target_id in seen:
        return (True, "active")
    if "." not in target_id.split(":")[0]:
        return (False, "foundation model — invoke 시 inference profile 강제 가능성")
    return (False, f"리전 {region}에 미등록 ({len(seen)}개 profile 조회됨)")


def test_model(
    label: str, slot: str, model_anthropic_id: str,
    user_prompt: str, max_tokens: int,
) -> dict:
    """단일 모델 검증."""
    bedrock_id = _BEDROCK_MODEL_MAP.get(model_anthropic_id, model_anthropic_id)
    print(f"\n--- {label} ({slot}) ---")
    print(f"  Anthropic ID : {model_anthropic_id}")
    print(f"  Bedrock ID   : {bedrock_id}")

    active, note = check_profile_active(bedrock_id)
    print(f"  Profile 활성 : {'✓' if active else '✗'} ({note})")
    if not active:
        return {
            "label": label, "ok": False, "stage": "profile_check",
            "error": note, "bedrock_id": bedrock_id, "slot": slot,
        }

    print(f"  호출 시도... (max_tokens={max_tokens})")
    try:
        out = chat(
            model=model_anthropic_id,
            system="간결하고 정확한 한국어로 답하세요.",
            user=user_prompt,
            max_tokens=max_tokens,
        )
        print(f"  응답         : {out!r}")
        return {"label": label, "ok": True, "response": out, "bedrock_id": bedrock_id}
    except Exception as e:
        print(f"  [FAIL] {type(e).__name__}: {str(e)[:200]}")
        return {
            "label": label, "ok": False, "stage": "invoke",
            "error": f"{type(e).__name__}: {str(e)[:300]}",
            "bedrock_id": bedrock_id, "slot": slot,
        }


def main() -> int:
    print("=" * 70)
    print(" Bedrock 동작 검증 — Haiku / Sonnet (도쿄 리전, 4.5 다운그레이드)")
    print("=" * 70)
    print(f"  USE_BEDROCK         : {SYS.use_bedrock}")
    print(f"  has_bedrock (AWS키) : {KEYS.has_bedrock}")
    print(f"  AWS region          : {KEYS.aws_region}")
    print(f"  매핑된 Bedrock IDs  :")
    for k, v in _BEDROCK_MODEL_MAP.items():
        print(f"    {k:32s} → {v}")

    if not SYS.use_bedrock:
        print("\n[ERROR] USE_BEDROCK=false. .env의 USE_BEDROCK=true 설정 후 다시 실행하세요.")
        return 1
    if not KEYS.has_bedrock:
        print("\n[ERROR] AWS 키 누락. AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION 확인.")
        return 1

    reset_usage()

    cases = [
        ("Haiku",  "HAIKU",  MODELS.discovery,
         "1+1은 얼마인가요? 숫자만 답하세요.", 50),
        ("Sonnet", "SONNET", MODELS.analysis,
         "한국어로 정확히 100자 분량으로 '제안서 자동화 시스템'에 대해 한 문단 작성하세요.",
         200),
    ]
    results = [test_model(*c) for c in cases]

    print()
    print("=" * 70)
    print(" 종합 결과")
    print("=" * 70)
    passed = sum(1 for r in results if r["ok"])
    for r in results:
        mark = "✅" if r["ok"] else "❌"
        line = f"  {mark} {r['label']:7s} → {r['bedrock_id']}"
        if not r["ok"]:
            line += f"  [{r.get('stage','?')}] {r['error'][:80]}"
        print(line)
    print()
    print(f"  통과: {passed}/{len(cases)}")
    print()
    usage = get_usage_summary()
    if usage:
        print("  토큰 사용량 (모델별):")
        for m, u in usage.items():
            print(f"    {m}: {u}")
        # 비용 추정 (jp. inference profile은 in-region rate 동일하다 가정)
        # Haiku 4.5: $1/1M in, $5/1M out (추정)
        # Sonnet 4.5: $3/1M in, $15/1M out (추정)
        rates = {
            "claude-haiku-4-5-20251001": (1.0, 5.0),
            "claude-sonnet-4-6":         (3.0, 15.0),
        }
        total = 0.0
        for m, u in usage.items():
            r_in, r_out = rates.get(m, (3.0, 15.0))
            cost = (u["in"] / 1_000_000) * r_in + (u["out"] / 1_000_000) * r_out
            total += cost
        print(f"  추정 비용: ~${total:.4f} USD")

    if passed < len(cases):
        print()
        print("=" * 70)
        print(" 실패 항목 디버깅 안내")
        print("=" * 70)
        for r in results:
            if not r["ok"] and r.get("slot"):
                print(_FAILURE_HINT.format(
                    bedrock_id=r["bedrock_id"],
                    region=KEYS.aws_region,
                    slot=r["slot"],
                ))
        return 1

    print(f"\n✅ {len(cases)}개 모델 전부 Bedrock으로 호출 성공")
    return 0


if __name__ == "__main__":
    sys.exit(main())
