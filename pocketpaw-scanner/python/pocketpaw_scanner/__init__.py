from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from . import _core


@dataclass(slots=True)
class NativeInjectionScanResult:
    threat_level: str
    matched_patterns: list[str]
    sanitized_content: str
    source: str


@dataclass(slots=True)
class NativePIIMatch:
    pii_type: str
    start: int
    end: int
    original: str


@dataclass(slots=True)
class NativeContentClassification:
    label: Literal["safe", "spam", "toxic"]
    score: float
    matched_rules: list[str]


def injection_detect(content: str, source: str = "unknown") -> NativeInjectionScanResult:
    threat_level, matched_patterns, sanitized_content, source_value = _core.injection_detect.scan(
        content, source
    )
    return NativeInjectionScanResult(
        threat_level=threat_level,
        matched_patterns=matched_patterns,
        sanitized_content=sanitized_content,
        source=source_value,
    )


def injection_normalize(content: str) -> str:
    return _core.injection_detect.normalize(content)


def pii_detect(content: str) -> list[NativePIIMatch]:
    return [NativePIIMatch(*row) for row in _core.pii_detect.scan(content)]


def content_classify(content: str) -> NativeContentClassification:
    label, score, rules = _core.content_classify.classify(content)
    return NativeContentClassification(label=label, score=score, matched_rules=rules)


__all__ = [
    "NativeInjectionScanResult",
    "NativePIIMatch",
    "NativeContentClassification",
    "injection_detect",
    "injection_normalize",
    "pii_detect",
    "content_classify",
]
