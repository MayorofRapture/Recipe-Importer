"""Conservative, source-independent checks for mixed recipe fields."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from recipe_quality import RecipeCandidate


@dataclass(frozen=True)
class RecipeValidationFinding:
    diagnostic_id: str
    field_path: str
    category: str
    severity: str
    explanation: str
    original_text: str
    suggested_action: str
    source_references: list[dict]
    heuristic: bool = True
    source_confirmed: bool = False


@dataclass(frozen=True)
class RepairProposal:
    ingredients: list[str]
    instructions: list[str]
    notes: list[str]
    uncertainties: list[str]
    inferred: bool = True


_INSTRUCTION_START = re.compile(
    r"^\s*(?:combine|mix|stir|beat|whisk|fold|preheat|bake|cook|heat|pour|spread|"
    r"press|place|add|remove|transfer|refrigerate|chill|cool|sprinkle|knead|simmer|boil)\b",
    re.IGNORECASE,
)
_SUBSTITUTION = re.compile(
    r"\b(?:may|can)\s+be\s+substituted\s+for\b|\bsubstitute\b.+\bfor\b",
    re.IGNORECASE,
)
_QUANTITY = re.compile(
    r"(?<!\w)(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?|[½⅓⅔¼¾⅛⅜⅝⅞])"
    r"\s*(?:cups?|tablespoons?|tbsp\.?|teaspoons?|tsp\.?|ounces?|oz\.?|pounds?|lbs?\.?|"
    r"grams?|g|ml|milliliters?|quarts?|pints?|packages?|cans?|cloves?)\b",
    re.IGNORECASE,
)
_PREPARATION_LOCATION = re.compile(
    r"\s+in\s+(?:(?:a|the)\s+)?(?:(?:small|medium|large)\s+)?"
    r"(?:mixing\s+)?(?:bowl|saucepan|pan|skillet|pot)\b",
    re.IGNORECASE,
)
_IMPERATIVE_AFTER_BOUNDARY = re.compile(
    r"[;.!]\s*(?:then\s+)?(?:beat|stir|mix|bake|cook|heat|pour|spread|fold|whisk|"
    r"combine|add|place|remove|cool|chill|refrigerate|sprinkle)\b",
    re.IGNORECASE,
)


def validate_recipe(recipe: RecipeCandidate) -> list[RecipeValidationFinding]:
    """Return high-precision warnings; never change the candidate."""
    findings: list[RecipeValidationFinding] = []
    for index, raw in enumerate(recipe.ingredients):
        value = str(raw).strip()
        if not value:
            continue
        field_path = f"ingredients[{index}]"
        if _SUBSTITUTION.search(value):
            findings.append(_finding(
                recipe, field_path, "substitution_in_ingredients", value,
                "This line describes a substitution and may belong in recipe notes rather than the main ingredient list.",
                "Review the source and move this wording to a clearly labeled substitution note if confirmed.",
            ))
            continue
        quantities = list(_QUANTITY.finditer(value))
        starts_as_instruction = bool(_INSTRUCTION_START.match(value))
        continues_as_instruction = bool(_IMPERATIVE_AFTER_BOUNDARY.search(value))
        if (starts_as_instruction and quantities) or (continues_as_instruction and len(quantities) >= 1):
            multiple = len(quantities) > 1
            detail = ("This ingredient row contains preparation wording and several measured ingredients."
                      if multiple else
                      "This ingredient row appears to contain a preparation instruction.")
            if not recipe.instructions:
                detail += " No separate instructions were extracted."
            findings.append(_finding(
                recipe, field_path, "instruction_in_ingredients", value, detail,
                ("Compare this row with the source. Separate supported ingredient entries from the original preparation wording and add any missing instructions."
                 if not recipe.instructions else
                 "Compare this row with the source. Separate supported ingredient entries from the original preparation wording."),
                severity="critical" if not recipe.instructions else "warning",
            ))
        elif len(quantities) >= 2 and re.search(r",|\band\b|\bor\b", value, re.IGNORECASE):
            findings.append(_finding(
                recipe, field_path, "possibly_combined_ingredients", value,
                "Several measured ingredient phrases appear in one row; check whether OCR combined separate entries.",
                "Compare the quantities and wording with the source before splitting this row.",
            ))
    return findings


def propose_repair(recipe: RecipeCandidate) -> RepairProposal:
    """Suggest a text-preserving split only when measured rows parse cleanly."""
    ingredients: list[str] = []
    instructions = list(recipe.instructions)
    notes = list(recipe.notes)
    uncertain: list[str] = []
    changed = False
    for raw in recipe.ingredients:
        value = str(raw).strip()
        if _SUBSTITUTION.search(value):
            notes.append(value)
            changed = True
            continue
        if not _INSTRUCTION_START.match(value):
            ingredients.append(value)
            continue
        matches = list(_QUANTITY.finditer(value))
        if len(matches) < 2:
            ingredients.append(value)
            uncertain.append(f"Could not safely separate measured ingredients from: {value}")
            continue
        content_end = len(value)
        location = _PREPARATION_LOCATION.search(value, matches[-1].end())
        if location:
            content_end = location.start()
        elif re.search(r"[;.!]", value[matches[-1].end():]):
            content_end = matches[-1].end() + re.search(r"[;.!]", value[matches[-1].end():]).start()
        else:
            ingredients.append(value)
            uncertain.append(f"Could not safely find the end of the ingredient list in: {value}")
            continue
        parts: list[str] = []
        for position, match in enumerate(matches):
            end = matches[position + 1].start() if position + 1 < len(matches) else content_end
            item = value[match.start():end].strip(" ,;\t\r\n")
            item = re.sub(r"\s+(?:and|or)\s*$", "", item, flags=re.IGNORECASE).strip()
            if not item or not re.match(r"^" + re.escape(match.group(0)), item, re.IGNORECASE):
                parts = []
                break
            parts.append(item)
        if len(parts) != len(matches):
            ingredients.append(value)
            uncertain.append(f"Could not safely split every measured ingredient in: {value}")
            continue
        ingredients.extend(parts)
        instructions.append(value)
        changed = True
    if not changed and not uncertain:
        uncertain.append("No safe structural repair was identified from the available text.")
    return RepairProposal(ingredients, instructions, notes, uncertain)


def _finding(recipe: RecipeCandidate, field_path: str, category: str, original: str,
             explanation: str, action: str, severity: str = "warning") -> RecipeValidationFinding:
    normalized = " ".join(original.casefold().split())
    digest = hashlib.sha256(f"{field_path}\0{category}\0{normalized}".encode("utf-8")).hexdigest()[:20]
    references = []
    index_match = re.fullmatch(r"ingredients\[(\d+)\]", field_path)
    index = int(index_match.group(1)) if index_match else -1
    ingredient_ranges = [item for item in recipe.source_line_ranges
                         if str(item.get("field", "")).split("[", 1)[0] == "ingredients"]
    if 0 <= index < len(ingredient_ranges):
        item = ingredient_ranges[index]
        references = [{key: item[key] for key in ("field", "start", "end") if key in item}]
    return RecipeValidationFinding(
        diagnostic_id=f"recipe-quality:{digest}", field_path=field_path,
        category=category, severity=severity, explanation=explanation,
        original_text=original, suggested_action=action,
        source_references=references,
    )
