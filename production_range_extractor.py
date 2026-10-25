"""Side-effect-free extraction adapter for verified cookbook page ranges."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from recipe_quality import ExtractionBatch, is_complete


def extract_range(context: dict, mode: str, config: dict, output_dir: Path,
                  router=None, before_provider_request: Callable[..., None] | None = None) -> list[dict]:
    """Extract candidates from the canonical range PDF without source-level persistence."""
    source = Path(context["input_path"])
    from hashlib import sha256
    if sha256(source.read_bytes()).hexdigest() != context["input_sha256"]:
        raise RuntimeError("Canonical range input changed before extraction.")

    import import_recipes
    import local_ocr
    from model_routing import ModelRouter, UsageLedger, parse_model_config
    from ocr_structuring import assemble_line_references

    local = None
    model_id = ""
    method = mode
    if mode in {"local_ocr", "ocr_assisted"}:
        local = local_ocr.extract_local(source, str(config.get("tesseract_cmd", "")))
        batch = import_recipes._prepare_python_ocr_batch(local)
        if mode == "ocr_assisted" and local.text.strip():
            if router is None:
                router = ModelRouter(
                    parse_model_config(config.get("models_json"), str(config.get("model", ""))),
                    UsageLedger(output_dir / "gemini_usage.json"),
                    global_rpm=float(config.get("global_rpm", 12)),
                )

            def structure(model):
                nonlocal model_id
                model_id = model.id
                return import_recipes.structure_ocr_text(
                    local.text, str(config["api_key"]), model.id,
                    before_request=before_provider_request,
                )

            routed = router.run(structure, retry_transient=False, reserve_requests=False)
            batch = assemble_line_references(local.text, routed.value)
            method = "ocr_assisted"
    elif mode == "gemini":
        if router is None:
            router = ModelRouter(
                parse_model_config(config.get("models_json"), str(config.get("model", ""))),
                UsageLedger(output_dir / "gemini_usage.json"),
                global_rpm=float(config.get("global_rpm", 12)),
            )

        def extract(model):
            nonlocal model_id
            model_id = model.id
            return import_recipes.extract(
                source, str(config["api_key"]), model.id,
                before_request=before_provider_request,
            )

        routed = router.run(extract, quality_check=import_recipes._quality_issue,
                            retry_transient=False, reserve_requests=False)
        batch = routed.value
    else:
        raise ValueError("Unsupported production range extraction mode.")

    rows = []
    for candidate in batch.recipes:
        rows.append({
            "status": "NEEDS_REVIEW" if candidate.requires_review or not is_complete(candidate) else "EXTRACTED",
            "recipe": candidate.model_dump(mode="json"),
            "extraction_method": method,
            "model": model_id,
            "diagnostics": [item.model_dump(mode="json") for item in candidate.missing_information],
            "warnings": list(batch.general_warnings) + list(candidate.warnings),
            "ocr": ({"engine": local.engine, "confidence": local.confidence,
                     "selected_variant": local.selected_variant,
                     "uncertainties": list(local.uncertainties),
                     "text": local.text, "original_text": local.original_text,
                     "layout_text": local.layout_text} if local else None),
        })
    if not rows:
        return [{"status": "NO_RECIPE", "recipe": None, "extraction_method": method,
                 "model": model_id, "warnings": list(batch.general_warnings),
                 "ocr": ({"engine": local.engine, "confidence": local.confidence,
                          "selected_variant": local.selected_variant,
                          "uncertainties": list(local.uncertainties),
                          "text": local.text, "original_text": local.original_text,
                          "layout_text": local.layout_text} if local else None)}]
    return rows
