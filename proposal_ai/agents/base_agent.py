"""
에이전트 추상 베이스 클래스.
모든 에이전트는 이 클래스를 상속받아 run() 메서드를 구현한다.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Callable, Optional

from loguru import logger


ProgressCallback = Callable[[str], None]


class BaseAgent(ABC):
    """
    공통 에이전트 인터페이스.

    하위 클래스 구현 규칙:
    - agent_name: 에이전트 식별자 (로깅/감사 기록에 사용)
    - run(): 메인 실행 로직 구현
    - progress(): 진행 상황 콜백 래퍼 사용
    """

    agent_name: str = "base"

    def __init__(self, progress_cb: Optional[ProgressCallback] = None) -> None:
        self._progress_cb = progress_cb or (lambda msg: None)
        self._start_time: float = 0.0

    def progress(self, msg: str) -> None:
        logger.info(f"[{self.agent_name}] {msg}")
        self._progress_cb(msg)

    def start_timer(self) -> None:
        self._start_time = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self._start_time

    @abstractmethod
    def run(self, *args, **kwargs):
        ...
