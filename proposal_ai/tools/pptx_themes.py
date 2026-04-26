"""PPTX 마스터 테마 3종.

각 테마는 색상 팔레트 + 폰트 + 톤 설명. 정디자가 발주처/사업 성격에 맞춰 1개 선택,
PPTX 빌더가 그 팔레트로 슬라이드를 그린다.
사용자가 회사 정보 탭에서 직접 .pptx를 올린 경우 그 .pptx가 우선 사용되며,
이때도 본 테마의 색상은 액센트/타이포 보정 용도로 활용된다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

from pptx.dml.color import RGBColor


@dataclass(frozen=True)
class Theme:
    key: str
    label: str
    tone: str
    primary: Tuple[int, int, int]
    accent: Tuple[int, int, int]
    warm: Tuple[int, int, int]
    soft: Tuple[int, int, int]
    text_dark: Tuple[int, int, int] = (0x1F, 0x29, 0x37)
    text_gray: Tuple[int, int, int] = (0x4B, 0x55, 0x63)
    font_title: str = "맑은 고딕"
    font_body: str = "맑은 고딕"

    def rgb(self, name: str) -> RGBColor:
        return RGBColor(*getattr(self, name))


THEMES: Dict[str, Theme] = {
    "corporate_navy": Theme(
        key="corporate_navy",
        label="공공기관용 네이비",
        tone="보수적·신뢰감 — 공공기관·대형 SI에 적합",
        primary=(0x0B, 0x2C, 0x5A),
        accent=(0x1E, 0x6F, 0xE6),
        warm=(0xF5, 0x8B, 0x00),
        soft=(0xE6, 0xF0, 0xFB),
    ),
    "innovation_orange": Theme(
        key="innovation_orange",
        label="혁신사업용 오렌지",
        tone="적극적·혁신 — AI·디지털전환·R&D 사업에 적합",
        primary=(0x1F, 0x29, 0x37),
        accent=(0xEA, 0x58, 0x0C),
        warm=(0xF5, 0xB7, 0x4D),
        soft=(0xFE, 0xF3, 0xC7),
    ),
    "minimal_white": Theme(
        key="minimal_white",
        label="미니멀 화이트",
        tone="깔끔·기술적 — 웹·모바일·플랫폼·UI/UX 사업에 적합",
        primary=(0x18, 0x18, 0x18),
        accent=(0x2B, 0x6C, 0xB0),
        warm=(0x6E, 0x9F, 0xC2),
        soft=(0xF3, 0xF6, 0xF9),
    ),
}


def get_theme(key: str | None) -> Theme:
    return THEMES.get((key or "corporate_navy").lower(), THEMES["corporate_navy"])


def hex_to_rgb(hex_str: str | None) -> Tuple[int, int, int] | None:
    if not hex_str:
        return None
    h = hex_str.strip().lstrip("#")
    if len(h) != 6:
        return None
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return None
