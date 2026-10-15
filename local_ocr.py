"""Optional local OCR recovery for Gemini RECITATION failures."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path

from ocr_structuring import LineSpan, segment_instruction_lines
from recipe_quality import Diagnostic, ExtractionBatch, RecipeCandidate


class OcrError(RuntimeError):
    """The local OCR attempt could not produce usable text."""


class OcrUnavailable(OcrError):
    """An optional OCR dependency or executable is unavailable."""


@dataclass(frozen=True)
class LayoutLine:
    text: str
    page: int = 1
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    confidence: float | None = None
    source: str = ""


@dataclass(frozen=True)
class OcrCapture:
    original_text: str
    layout_text: str
    layout_lines: tuple[LayoutLine, ...] = ()
    engine: str = "tesseract"


@dataclass(frozen=True)
class OcrResult:
    text: str
    engine: str
    batch: ExtractionBatch
    manual_review_required: bool = True
    note: str = "Local OCR is source-derived text and requires human review before import."
    structured: bool = False
    structuring_method: str = "python"
    confidence: str = "low"
    uncertainties: tuple[str, ...] = ()
    layout_lines: tuple[LayoutLine, ...] = ()
    original_text: str = ""
    layout_text: str = ""
    selected_variant: str = "original"

    @classmethod
    def from_text(cls, text: str, engine: str = "tesseract",
                  layout_lines: tuple[LayoutLine, ...] = ()) -> "OcrResult":
        return cls.from_variants(text, text, engine=engine, layout_lines=layout_lines)

    @classmethod
    def from_variants(cls, original_text: str, layout_text: str,
                      engine: str = "tesseract",
                      layout_lines: tuple[LayoutLine, ...] = ()) -> "OcrResult":
        selected_text, selected_variant, selection_warnings = _select_ocr_variant(
            original_text, layout_text)
        batch = _batch_from_text(selected_text)
        sections = _sections(selected_text)
        structured = bool(batch.recipes and len(sections) == len(batch.recipes) and all(
            candidate.name.strip() and candidate.ingredients and candidate.instructions
            and _has_ingredient_heading(body) and _has_instruction_heading(body)
            for candidate, (_, body) in zip(batch.recipes, sections)
        ))
        uncertainty = tuple(selection_warnings) + (() if structured else (
            "Deterministic OCR parsing did not identify all recipe sections confidently.",
        ))
        for candidate in batch.recipes:
            _append_quality_diagnostics(candidate, selected_text)
            if selection_warnings:
                candidate.warnings.extend(selection_warnings)
                candidate.missing_information.append(Diagnostic(
                    field_path="recipe",
                    kind="ambiguous",
                    detail="Layout OCR was not selected as authoritative because its reading order was less reliable.",
                    suggested_action="Compare the preserved original and layout OCR text against the source image.",
                ))
                for warning in selection_warnings:
                    if warning.startswith("Layout OCR quality check:"):
                        candidate.missing_information.append(Diagnostic(
                            field_path="recipe", kind="ambiguous", detail=warning,
                            suggested_action="Inspect both OCR variants against the source image before approval.",
                        ))
            candidate.source_method = "python"
            candidate.confidence = "high" if structured and not selection_warnings \
                else "medium" if structured else "low"
            candidate.uncertainties = list(uncertainty)
            candidate.requires_review = True
        return cls(text=selected_text, engine=engine, batch=batch, structured=structured,
                   confidence="high" if structured and not selection_warnings
                   else "medium" if structured else "low",
                   uncertainties=uncertainty, layout_lines=layout_lines,
                   original_text=original_text, layout_text=layout_text,
                   selected_variant=selected_variant)


def _load_pymupdf():
    try:
        import fitz
    except ImportError as exc:
        raise OcrUnavailable("PyMuPDF is not installed; install requirements-ocr.txt") from exc
    return fitz


def _load_tesseract():
    try:
        import pytesseract
    except ImportError as exc:
        raise OcrUnavailable("pytesseract is not installed; install requirements-ocr.txt") from exc
    return pytesseract


def _load_pil():
    try:
        from PIL import Image
    except ImportError as exc:
        raise OcrUnavailable("Pillow is not installed; install requirements-ocr.txt") from exc
    return Image


def _configure_tesseract(pytesseract, command: str) -> None:
    if not command:
        return
    target = getattr(pytesseract, "pytesseract", pytesseract)
    setattr(target, "tesseract_cmd", command)


def _tesseract_text(image, command: str = "") -> str:
    pytesseract = _load_tesseract()
    _configure_tesseract(pytesseract, command)
    try:
        return str(pytesseract.image_to_string(image) or "")
    except (FileNotFoundError, OSError) as exc:
        raise OcrUnavailable("Tesseract executable was not found; set TESSERACT_CMD") from exc
    except Exception as exc:
        if "tesseract" in str(exc).lower() or "executable" in str(exc).lower():
            raise OcrUnavailable(f"Tesseract OCR failed: {exc}") from exc
        raise OcrError(f"Tesseract OCR failed: {exc}") from exc


def _layout_order(lines: list[LayoutLine]) -> list[LayoutLine]:
    """Read stable coordinate regions without sorting unrelated blocks by y."""
    ordered: list[LayoutLine] = []
    for page in sorted({line.page for line in lines}):
        page_lines = [line for line in lines if line.page == page]
        if not page_lines:
            continue
        page_left = min(line.x0 for line in page_lines)
        page_right = max(line.x1 for line in page_lines)
        page_span = max(page_right - page_left, 1.0)
        full_width: list[LayoutLine] = []
        body: list[LayoutLine] = []
        for line in page_lines:
            width = max(line.x1 - line.x0, 0.0)
            if (width >= page_span * 0.72
                    or (line.x0 <= page_left + page_span * 0.04
                        and line.x1 >= page_right - page_span * 0.04)):
                full_width.append(line)
            else:
                body.append(line)
        ordered.extend(sorted(full_width, key=lambda line: (line.y0, line.x0)))
        if not body:
            continue

        # A column is a horizontal region, not a Tesseract block. Small x drift
        # between lines therefore cannot reorder numbered steps.
        by_x = sorted(body, key=lambda line: (line.x0, line.x1, line.y0))
        columns: list[list[LayoutLine]] = []
        column_right = None
        separation = max(30.0, page_span * 0.045)
        for line in by_x:
            if column_right is None or line.x0 > column_right + separation:
                columns.append([line])
                column_right = line.x1
            else:
                columns[-1].append(line)
                column_right = max(column_right, line.x1)
        for column in columns:
            ordered.extend(sorted(column, key=lambda line: (line.y0, line.x0)))
    return ordered


def _tesseract_layout(image, command: str = "") -> OcrCapture:
    """Extract Tesseract words into ordered lines while retaining their boxes."""
    pytesseract = _load_tesseract()
    _configure_tesseract(pytesseract, command)
    original_text = _tesseract_text(image, command)
    image_to_data = getattr(pytesseract, "image_to_data", None)
    if not callable(image_to_data):
        return OcrCapture(original_text, original_text, (), "tesseract")
    try:
        output_type = getattr(getattr(pytesseract, "Output", None), "DICT", "dict")
        data = image_to_data(image, output_type=output_type)
        grouped: dict[tuple[int, int, int], list[LayoutLine]] = {}
        texts = data.get("text", [])
        for index, value in enumerate(texts):
            text = str(value or "").strip()
            if not text:
                continue
            def field(name: str, default):
                values = data.get(name, [])
                return values[index] if index < len(values) else default

            block = int(field("block_num", 0) or 0)
            paragraph = int(field("par_num", 0) or 0)
            line_number = int(field("line_num", index) or index)
            x0 = float(field("left", 0) or 0)
            y0 = float(field("top", 0) or 0)
            width = float(field("width", 0) or 0)
            height = float(field("height", 0) or 0)
            raw_conf = field("conf", "")
            try:
                confidence = float(raw_conf)
            except (TypeError, ValueError):
                confidence = None
            grouped.setdefault((block, paragraph, line_number), []).append(
                LayoutLine(text=text, x0=x0, y0=y0, x1=x0 + width,
                           y1=y0 + height, confidence=confidence,
                           source=f"tesseract-block:{block}"))
        lines = []
        for words in grouped.values():
            words.sort(key=lambda word: word.x0)
            word_gaps = [(words[index + 1].x0 - words[index].x1, index)
                         for index in range(len(words) - 1)]
            gap, split = max(word_gaps, default=(0.0, -1))
            word_span = max(word.x1 for word in words) - min(word.x0 for word in words)
            chunks = (words[:split + 1], words[split + 1:]
                      ) if split >= 1 and gap >= max(40.0, word_span * 0.25) else [words]
            for chunk in chunks:
                if not chunk:
                    continue
                confidences = [word.confidence for word in chunk
                               if word.confidence is not None]
                lines.append(LayoutLine(
                    text=" ".join(word.text for word in chunk),
                    x0=min(word.x0 for word in chunk), y0=min(word.y0 for word in chunk),
                    x1=max(word.x1 for word in chunk), y1=max(word.y1 for word in chunk),
                    confidence=sum(confidences) / len(confidences) if confidences else None,
                    source=chunk[0].source,
                ))
        if not lines:
            return OcrCapture(original_text, original_text, (), "tesseract")
        ordered = _layout_order(lines)
        return OcrCapture(original_text, "\n".join(line.text for line in ordered),
                          tuple(ordered), "tesseract")
    except (FileNotFoundError, OSError) as exc:
        raise OcrUnavailable("Tesseract executable was not found; set TESSERACT_CMD") from exc
    except Exception as exc:
        if "tesseract" in str(exc).lower() or "executable" in str(exc).lower():
            raise OcrUnavailable(f"Tesseract OCR failed: {exc}") from exc
        raise OcrError(f"Tesseract layout OCR failed: {exc}") from exc


def _ocr_image(path: Path, tesseract_cmd: str) -> OcrCapture:
    Image = _load_pil()
    try:
        with Image.open(path) as image:
            return _tesseract_layout(image, tesseract_cmd)
    except OcrError:
        raise
    except Exception as exc:
        raise OcrError(f"Could not open image for OCR: {exc}") from exc


def _pdf_layout_lines(page, page_number: int) -> list[LayoutLine]:
    try:
        data = page.get_text("dict")
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    lines: list[LayoutLine] = []
    for block_index, block in enumerate(data.get("blocks", [])):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            spans = [str(span.get("text", "")) for span in line.get("spans", [])]
            text = "".join(spans).strip()
            if not text:
                continue
            bbox = line.get("bbox", block.get("bbox", [0, 0, 0, 0]))
            lines.append(LayoutLine(
                text=text, page=page_number,
                x0=float(bbox[0]), y0=float(bbox[1]),
                x1=float(bbox[2]), y1=float(bbox[3]),
                source=f"pymupdf-block:{block_index}",
            ))
    return _layout_order(lines)


def _ocr_pdf(path: Path, tesseract_cmd: str) -> OcrCapture:
    fitz = _load_pymupdf()
    document = fitz.open(str(path))
    original_pages: list[str] = []
    layout_pages: list[str] = []
    layout_lines: list[LayoutLine] = []
    used_ocr = False
    try:
        for number in range(len(document)):
            page = document[number]
            selectable = str(page.get_text("text") or "").strip()
            if selectable:
                page_lines = _pdf_layout_lines(page, number + 1)
                if not page_lines:
                    page_lines = [LayoutLine(text=line, page=number + 1,
                                             source=f"pymupdf-page:{number + 1}")
                                  for line in selectable.splitlines() if line.strip()]
                ordered_text = "\n".join(line.text for line in page_lines)
                original_pages.append(f"--- PDF PAGE {number + 1} ---\n{selectable}")
                layout_pages.append(f"--- PDF PAGE {number + 1} ---\n{ordered_text or selectable}")
                layout_lines.extend(page_lines)
                continue
            used_ocr = True
            Image = _load_pil()
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            image = Image.open(io.BytesIO(pixmap.tobytes("png")))
            try:
                capture = _tesseract_layout(image, tesseract_cmd)
                original_text = capture.original_text.strip()
                layout_text = capture.layout_text.strip()
            finally:
                close = getattr(image, "close", None)
                if close:
                    close()
            if original_text:
                original_pages.append(f"--- OCR PAGE {number + 1} ---\n{original_text}")
            if layout_text:
                layout_pages.append(f"--- OCR PAGE {number + 1} ---\n{layout_text}")
                layout_lines.extend(LayoutLine(
                    text=line.text, page=number + 1, x0=line.x0, y0=line.y0,
                    x1=line.x1, y1=line.y1, confidence=line.confidence,
                    source=line.source,
                ) for line in capture.layout_lines)
    finally:
        close = getattr(document, "close", None)
        if close:
            close()
    return OcrCapture(
        original_text="\n\n".join(original_pages).strip(),
        layout_text="\n\n".join(layout_pages).strip(),
        layout_lines=tuple(layout_lines),
        engine="pymupdf-text+tesseract" if used_ocr else "pymupdf-text",
    )


_RECIPE_MARKER = re.compile(r"^\s*RECIPE\s*(?:\d+)?\s*[:.-]\s*(.+?)\s*$", re.IGNORECASE)
_HEADING = re.compile(r"^\s*(ingredients?|instructions?|directions?|method|preparation)\s*:?[ \t]*$", re.IGNORECASE)
_DATE_PATTERN = r"(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}\.\d{1,2}\.\d{2,4})"
_MONTH_DATE_PATTERN = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:,\s*|\s+)\d{4}"
_TIME_PATTERN = r"\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AP]M)?"
_DATE_OR_TIME = re.compile(
    rf"^\s*(?:(?:{_DATE_PATTERN}|{_MONTH_DATE_PATTERN})(?:\s+{_TIME_PATTERN})?|{_TIME_PATTERN})\s*$",
    re.IGNORECASE,
)
_PAGE_NUMBER = re.compile(r"^\s*(?:page\s+\d{1,6}(?:\s+of\s+\d{1,6})?|\d{1,3})\s*$", re.IGNORECASE)
_SCAN_HEADER = re.compile(
    r"^\s*(?:scan(?:ned)?|img|image|photo|dsc|document)(?:$|[_-]|\s+\d|\s+(?:image|scan)\b|\.[A-Za-z0-9]+$)",
    re.IGNORECASE,
)
_PAGE_MARKER = re.compile(r"^\s*---\s*(?:OCR|PDF)\s+PAGE\s+\d+\s*---\s*$", re.IGNORECASE)
_WEBSITE_NOISE = re.compile(
    r"(?:allrecipes|log\s*in|sign\s*up|saved\s+recipes|search\s+recipes|"
    r"print\s+recipe|jump\s+to\s+recipe|subscribe|navigation)",
    re.IGNORECASE,
)
_STEP_LINE = re.compile(r"^\s*(?:step\s*)?\d{1,3}(?:\s*[.):\-]|\s*$)", re.IGNORECASE)
_TITLE_TRAILING_NOISE = re.compile(r"\s+(?:save|print|share)\s*$", re.IGNORECASE)


def _is_ignored_title_line(line: str) -> bool:
    """Identify scan metadata without rejecting ordinary numeric recipe titles."""
    return bool(_DATE_OR_TIME.fullmatch(line) or _PAGE_NUMBER.fullmatch(line)
                or _SCAN_HEADER.match(line) or _PAGE_MARKER.fullmatch(line)
                or _WEBSITE_NOISE.search(line))


def _is_ignored_content_line(line: str) -> bool:
    return _is_ignored_title_line(line)


def _clean_title_candidate(line: str) -> str:
    return _TITLE_TRAILING_NOISE.sub("", line.strip()).strip()


def _title_like(line: str) -> bool:
    value = _clean_title_candidate(line)
    if not value or _is_ignored_title_line(value) or _STEP_LINE.match(value):
        return False
    if len(value) > 80 or any(mark in value for mark in ".,;:!?/"):
        return False
    words = re.findall(r"[^\W_]+(?:['&-][^\W_]+)*", value, re.UNICODE)
    if not 1 <= len(words) <= 8:
        return False
    capitalized = sum(1 for word in words if word[0].isupper() or word.isdigit())
    return value.isupper() or capitalized >= max(1, len(words) // 2)


def _looks_corrupted_value(value: str) -> bool:
    return "�" in value or "??" in value or "%" in value


def _find_title(lines: list[tuple[int, str]]) -> tuple[
        str, tuple[int, int] | None, list[tuple[int, str]]]:
    candidates: list[tuple[float, int, str]] = []
    for position, (_, raw) in enumerate(lines):
        line = raw.strip()
        if _HEADING.match(line):
            break
        value = _clean_title_candidate(line)
        if not _title_like(value):
            continue
        words = re.findall(r"[^\W_]+(?:['&-][^\W_]+)*", value, re.UNICODE)
        capitalized = sum(1 for word in words if word[0].isupper() or word.isdigit())
        score = capitalized * 2 + (2 if len(words) >= 2 else 0) - position * 0.02
        candidates.append((score, position, value))
    if not candidates:
        return "", None, lines
    _, position, value = max(candidates, key=lambda item: (item[0], -item[1]))
    title_parts = [value]
    title_start = lines[position][0]
    title_end = title_start
    next_position = position + 1
    while next_position < len(lines) and len(title_parts) < 3:
        next_line = lines[next_position][1].strip()
        if not next_line or _HEADING.match(next_line) or not _title_like(next_line):
            break
        title_parts.append(_clean_title_candidate(next_line))
        title_end = lines[next_position][0]
        next_position += 1
    return " ".join(title_parts), (title_start, title_end), lines[next_position:]


def _has_ingredient_heading(text: str) -> bool:
    return any((match := _HEADING.match(line.strip()))
               and match.group(1).casefold().startswith("ingredient")
               for line in text.splitlines())


def _has_instruction_heading(text: str) -> bool:
    return any((match := _HEADING.match(line.strip()))
               and not match.group(1).casefold().startswith("ingredient")
               for line in text.splitlines())


def _sections(text: str) -> list[tuple[str, str]]:
    return [(title, body) for title, body, _, _ in _section_records(text)]


def _section_records(text: str) -> list[tuple[str, str, int, tuple[int, int] | None]]:
    matches = [(match, index) for index, line in enumerate(text.splitlines())
               if (match := _RECIPE_MARKER.match(line))]
    if not matches:
        return [("", text, 1, None)]
    lines = text.splitlines()
    sections: list[tuple[str, str, int, tuple[int, int] | None]] = []
    for position, (match, line_index) in enumerate(matches):
        end = matches[position + 1][1] if position + 1 < len(matches) else len(lines)
        sections.append((
            match.group(1).strip(),
            "\n".join(lines[line_index + 1:end]),
            line_index + 2,
            (line_index + 1, line_index + 1),
        ))
    return sections


def _candidate(title: str, body: str, index: int, body_start_line: int = 1,
               title_line: tuple[int, int] | None = None) -> RecipeCandidate:
    lines = [(body_start_line + offset, line)
             for offset, line in enumerate(body.splitlines())]
    if not title:
        title, title_line, lines = _find_title(lines)
    ingredients: list[str] = []
    ingredient_ranges: list[LineSpan] = []
    instructions: list[str] = []
    instruction_lines: list[tuple[int, str]] = []
    skipped_artifacts: list[str] = []
    area = ""
    for line_number, raw in lines:
        line = raw.strip()
        heading = _HEADING.match(line)
        if heading:
            label = heading.group(1).casefold()
            area = "ingredients" if label.startswith("ingredient") else "instructions"
            continue
        if not line:
            if area == "instructions":
                instruction_lines.append((line_number, ""))
            continue
        if _is_ignored_content_line(line):
            skipped_artifacts.append(line)
            if area == "instructions":
                instruction_lines.append((line_number, ""))
            continue
        if area == "ingredients":
            ingredients.append(line.lstrip("-•* "))
            ingredient_ranges.append(LineSpan(start=line_number, end=line_number))
        elif area == "instructions":
            instruction_lines.append((line_number, raw))
    instruction_parts = segment_instruction_lines(instruction_lines)
    instructions = [value for value, _ in instruction_parts]
    title = title.strip() or f"OCR section {index}"
    detail = "OCR transcription and recipe boundaries require human verification before import."
    kind = "not_extracted" if ingredients and instructions else "ambiguous"
    findings = [Diagnostic(
        field_path="recipe",
        kind=kind,
        detail=detail,
        suggested_action="Inspect the saved OCR text, then edit or approve the recipe before importing.",
    )]
    if title_line is None:
        findings.append(Diagnostic(
            field_path="name",
            kind="ambiguous",
            detail="No reliable recipe title was identified in the OCR layout.",
            suggested_action="Review the source and enter a title before importing.",
        ))
    for ingredient_index, ingredient in enumerate(ingredients):
        if _looks_corrupted_value(ingredient):
            findings.append(Diagnostic(
                field_path=f"ingredients[{ingredient_index}]",
                kind="ambiguous",
                detail="The OCR measurement contains corruption; no correction was inferred.",
                source_evidence=ingredient,
                suggested_action="Verify the quantity and unit against the source image.",
            ))
    uncertain_instruction_lines = [line for _, line in instruction_lines
                                   if _looks_corrupted_value(line)]
    if uncertain_instruction_lines:
        findings.append(Diagnostic(
            field_path="instructions",
            kind="ambiguous",
            detail="One or more OCR instruction lines appear corrupted; wording was not corrected.",
            source_evidence=" | ".join(uncertain_instruction_lines),
            suggested_action="Verify the affected instructions against the source image.",
        ))
    source_line_ranges: list[dict] = []
    if title_line is not None:
        source_line_ranges.append({"field": "name", "start": title_line[0], "end": title_line[1]})
    source_line_ranges.extend({"field": "ingredients", "start": span.start, "end": span.end}
                              for span in ingredient_ranges)
    source_line_ranges.extend({"field": "instructions", "start": span.start, "end": span.end}
                              for _, span in instruction_parts)
    return RecipeCandidate(
        name=title,
        ingredients=ingredients,
        instructions=instructions,
        source_method="python",
        source_line_ranges=source_line_ranges,
        warnings=(['Ignored known OCR/layout artifact lines from structured fields.']
                  if skipped_artifacts else []),
        missing_information=findings,
    )


def _batch_from_text(text: str) -> ExtractionBatch:
    candidates = [_candidate(title, body, index, body_start_line, title_line)
                  for index, (title, body, body_start_line, title_line)
                  in enumerate(_section_records(text), start=1)]
    return ExtractionBatch(
        recipes=candidates,
        general_warnings=["Recovered with local OCR; all fields require human review."],
    )


def _step_numbers(candidate: RecipeCandidate) -> list[int]:
    numbers: list[int] = []
    for instruction in candidate.instructions:
        match = re.match(r"^\s*(?:step\s*)?(\d{1,3})(?:\s*[.):\-]|\s+)", instruction,
                         re.IGNORECASE)
        if match:
            numbers.append(int(match.group(1)))
    return numbers


def _looks_like_mixed_line(line: str) -> bool:
    return bool(re.search(
        r"^\s*(?:[%\d½¼¾⅓⅔⅛⅜⅝⅞]|\d+\s*[A-Za-z])[^\n]*\b(?:heat|bake|combine|stir|mix|press|pour|cook|cool|sprinkle|add|reserve|top|cut|place)\b",
        line, re.IGNORECASE,
    ))


def _variant_issues(text: str, batch: ExtractionBatch) -> list[str]:
    issues: list[str] = []
    if not batch.recipes:
        return ["no recipe candidate"]
    if any(_looks_like_mixed_line(line) for line in text.splitlines()):
        issues.append("mixed ingredient/instruction lines")
    for candidate in batch.recipes:
        words = candidate.name.split()
        if (not candidate.name.strip() or candidate.name.startswith("OCR section")
                or len(words) > 8 or _WEBSITE_NOISE.search(candidate.name)
                or any(mark in candidate.name for mark in ",;:")):
            issues.append("implausible or placeholder title")
        numbers = _step_numbers(candidate)
        if numbers and numbers != sorted(numbers):
            issues.append("out-of-order numbered steps")
        if candidate.instructions:
            short = sum(len(value.split()) <= 3 for value in candidate.instructions)
            if len(candidate.instructions) >= 4 and short * 2 > len(candidate.instructions):
                issues.append("suspiciously fragmented instructions")
        if any(_looks_corrupted_value(value) or "%" in value
               for value in candidate.ingredients):
            issues.append("corrupted or ambiguous ingredient quantity")
    return list(dict.fromkeys(issues))


def _variant_score(text: str, batch: ExtractionBatch, issues: list[str]) -> float:
    score = min(len(text.strip()) / 500.0, 3.0)
    for candidate in batch.recipes:
        score += 4 if candidate.name.strip() and not candidate.name.startswith("OCR section") else -4
        score += 3 if candidate.ingredients else -3
        score += 3 if candidate.instructions else -3
        score += 1 if _has_ingredient_heading(text) else 0
        score += 1 if _has_instruction_heading(text) else 0
    return score - (len(issues) * 4)


def _select_ocr_variant(original_text: str, layout_text: str) -> tuple[str, str, tuple[str, ...]]:
    if not layout_text.strip() or layout_text == original_text:
        return original_text, "original", ()
    original_batch = _batch_from_text(original_text)
    layout_batch = _batch_from_text(layout_text)
    original_issues = _variant_issues(original_text, original_batch)
    layout_issues = _variant_issues(layout_text, layout_batch)
    original_score = _variant_score(original_text, original_batch, original_issues)
    layout_score = _variant_score(layout_text, layout_batch, layout_issues)
    if layout_score > original_score:
        warnings = tuple(f"Layout OCR quality check: {issue}." for issue in layout_issues)
        return layout_text, "layout", warnings
    warnings = tuple(
        ["Layout OCR was rejected because its quality score was not better than the original OCR."]
        + [f"Layout OCR quality check: {issue}." for issue in layout_issues]
    )
    return original_text, "original", warnings


def _append_quality_diagnostics(candidate: RecipeCandidate, text: str) -> None:
    existing = {item.detail for item in candidate.missing_information}
    numbers = _step_numbers(candidate)
    if numbers and numbers != sorted(numbers):
        detail = "Numbered instruction steps are out of order in the OCR text."
        if detail not in existing:
            candidate.missing_information.append(Diagnostic(
                field_path="instructions", kind="ambiguous", detail=detail,
                suggested_action="Verify step order against the source image; no steps were reordered automatically.",
            ))
    if any(_looks_like_mixed_line(line) for line in text.splitlines()):
        detail = "OCR lines contain both ingredient-like quantities and instruction wording."
        if detail not in existing:
            candidate.missing_information.append(Diagnostic(
                field_path="recipe", kind="ambiguous", detail=detail,
                suggested_action="Separate fields manually using the source image before approval.",
            ))
    if any(_looks_corrupted_value(value) or "%" in value
           for value in candidate.ingredients):
        detail = "The OCR measurement contains corruption or a percent-like token; no fraction was inferred."
        if detail not in existing:
            candidate.missing_information.append(Diagnostic(
                field_path="ingredients", kind="ambiguous", detail=detail,
                suggested_action="Verify every quantity and unit against the source image.",
            ))


def extract_local(path: Path, tesseract_cmd: str = "") -> OcrResult:
    suffix = path.suffix.casefold()
    if suffix == ".pdf":
        capture = _ocr_pdf(path, tesseract_cmd)
    elif suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        capture = _ocr_image(path, tesseract_cmd)
    else:
        raise OcrError(f"Unsupported local OCR source: {path.suffix}")
    if not capture.original_text.strip() and not capture.layout_text.strip():
        raise OcrError("OCR returned no text")
    return OcrResult.from_variants(
        capture.original_text, capture.layout_text, capture.engine, capture.layout_lines)
