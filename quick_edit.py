"""Pure Quick Edit parsing and suspicious-content warnings."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ParseWarning:
    field_path: str
    kind: str
    message: str
    severity: str = "warning"


@dataclass(frozen=True)
class ParseResult:
    items: list[str]
    warnings: list[ParseWarning]


_PUNCTUATION_ONLY = re.compile(r"^[^\w\d\s]+$", re.UNICODE)
_DATE_LIKE = re.compile(r"(?<!\d)(?:\d+\s+){2,}\d{2,}(?!\d)")
_STEP_ONLY = re.compile(r"^step\s*\d+\s*[:.)-]?\s*$", re.IGNORECASE)


def parse_ingredients(text: str) -> ParseResult:
    items = [line.strip() for line in text.splitlines() if line.strip()]
    warnings: list[ParseWarning] = []
    for index, item in enumerate(items):
        if _PUNCTUATION_ONLY.fullmatch(item) or item.casefold() in {"%", "??", "�"}:
            warnings.append(ParseWarning(
                f"ingredients[{index}]", "ocr_noise",
                "This line looks like OCR noise rather than a complete ingredient.",
            ))
        elif _DATE_LIKE.search(item) and any(char.isdigit() for char in item):
            warnings.append(ParseWarning(
                f"ingredients[{index}]", "ocr_noise",
                "This line looks like OCR noise or scan metadata; check it against the source.",
            ))
        elif _looks_like_instruction(item):
            warnings.append(ParseWarning(
                f"ingredients[{index}]", "mixed_content",
                "This ingredient line may contain an instruction; check the source section.",
            ))
    return ParseResult(items, warnings)


def parse_instructions(text: str) -> ParseResult:
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)

    items = [" ".join(lines) for lines in blocks]
    warnings: list[ParseWarning] = []
    for index, item in enumerate(items):
        if _STEP_ONLY.fullmatch(item):
            warnings.append(ParseWarning(
                f"instructions[{index}]", "fragmented_instruction",
                "This step contains only a step label; add or verify the cooking action.",
            ))
        elif len(item) < 4:
            warnings.append(ParseWarning(
                f"instructions[{index}]", "fragmented_instruction",
                "This instruction is unusually short; check its boundary against the source.",
            ))
    return ParseResult(items, warnings)


def to_quick_edit(ingredients: list[str], instructions: list[str]) -> tuple[str, str]:
    return "\n".join(ingredients), "\n\n".join(instructions)


def _looks_like_instruction(value: str) -> bool:
    lowered = value.casefold()
    return bool(re.search(r"\b(?:preheat|bake|cook|stir|mix|combine|remove|pour)\b", lowered))
