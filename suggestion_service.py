"""Explicit, durable on-demand recipe suggestion requests."""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from recipe_quality import Suggestion
from review_store import ReviewConflict, ReviewStore, _recipe_digest

class SuggestionService:
    """Keep provider waits outside the review ledger lock and never retry unknown calls."""

    def __init__(self, output_dir: Path | str, *, router: Any = None,
                 proposer: Callable[[dict, str], list[dict]] | None = None,
                 store: ReviewStore | None = None):
        self.output_dir = Path(output_dir)
        self.store = store or ReviewStore(self.output_dir / "review_events.jsonl")
        self.router = router
        self.proposer = proposer
        # Queued requests remain discoverable and may be resumed with a single-winner
        # atomic claim. Running requests are interrupted only when their worker is gone.
        self.store.recover_interrupted_suggestion_requests()

    def request(self, view: Any, suggestion_type: str = "instructions", *,
                consent: bool = False, operation_id: str | None = None) -> dict:
        get = (lambda name, default=None: view.get(name, default)) if isinstance(view, dict) else (
            lambda name, default=None: getattr(view, name, default))
        if not consent:
            raise ValueError("explicit cloud consent is required")
        if get("protected", False):
            raise ReviewConflict("protected recipes cannot request generated suggestions")
        if not get("source_verified", False):
            raise ReviewConflict("the original source must be verified before requesting a suggestion")
        candidate = get("candidate")
        if candidate is None:
            raise ValueError("recipe content is required")
        candidate_data = candidate.model_dump(mode="json") if hasattr(candidate, "model_dump") else dict(candidate)
        recipe = {key: candidate_data.get(key) for key in ("name", "ingredients", "instructions")}
        digest = str(get("content_digest", "") or _recipe_digest(recipe))
        bindings = list(get("revision_target_bindings", get("target_bindings", ())) or ())
        instruction_bindings = [item for item in bindings if item.get("field") == "instructions"]
        return self.store.create_suggestion_request(
            candidate_id=str(get("candidate_id", "")), source_hash=str(get("source_hash", "")),
            extraction_id=str(get("extraction_id", "")), revision=int(get("revision", 0)),
            content_digest=digest, recipe_snapshot=recipe,
            suggestion_type=suggestion_type, operation_id=operation_id or "suggestion-request",
            consent=True,
            target_bindings=instruction_bindings,
        )

    def status(self, request_id: str) -> dict | None:
        return self.store.get_suggestion_request(request_id)

    def latest_request(self, candidate_id: str, source_hash: str,
                       extraction_id: str) -> dict | None:
        return self.store.latest_suggestion_request(candidate_id, source_hash, extraction_id)

    def cancel(self, request_id: str) -> None:
        self.store.cancel_suggestion_request(str(request_id))

    def process(self, request_id: str) -> dict:
        request = self.store.get_suggestion_request(request_id)
        if not request:
            raise ReviewConflict("suggestion request was not found")
        if request.get("status") != "queued":
            return request
        worker_claim_id = uuid.uuid4().hex
        request = self.store.claim_suggestion_request(request_id, worker_claim_id)
        if not request or request.get("worker_claim_id") != worker_claim_id:
            return request or {}
        try:
            proposer = self.proposer or self._configured_proposer()
            provider_snapshot = dict(request["recipe_snapshot"])
            provider_snapshot["instruction_targets"] = request.get("instruction_targets", [])
            result = proposer(provider_snapshot, request["suggestion_type"])
            current = self.store.get_suggestion_request(request_id)
            if not current or current.get("status") != "running":
                return current
            proposals = []
            for item in result:
                proposal = dict(item)
                if proposal.get("classification", "inferred") != "inferred":
                    raise ValueError("text-only instruction requests may produce inferred proposals only")
                proposal["classification"] = "inferred"
                proposal.setdefault("model_id", getattr(self.router, "last_model_id", ""))
                proposals.append(proposal)
            if not proposals:
                return self.store.update_suggestion_request(request_id, "completed")
            self.store.record_dynamic_proposals(request_id, proposals)
        except Exception as exc:
            status = "quota_blocked" if any(word in str(exc).casefold()
                                               for word in ("quota", "429", "resource_exhausted")) else "failed"
            try:
                self.store.update_suggestion_request(request_id, status, error=_safe_error(exc))
            except ReviewConflict:
                pass
        finally:
            # If control flow exits outside Exception (for example, thread
            # shutdown), preserve the uncertain provider outcome without
            # converting or swallowing the original BaseException.
            try:
                current = self.store.get_suggestion_request(request_id)
                if current and current.get("status") == "running":
                    self.store.update_suggestion_request(
                        request_id, "interrupted",
                        error="Worker stopped during provider request; create a new authorized request.",
                    )
            except Exception:
                pass
        return self.store.get_suggestion_request(request_id)

    def start_background(self, request_id: str) -> threading.Thread:
        thread = threading.Thread(target=self.process, args=(request_id,),
                                  name=f"recipe-suggestion-{request_id[:8]}", daemon=True)
        thread.start()
        return thread

    def resume(self, request_id: str) -> threading.Thread:
        """Explicitly resume a durably queued request; atomic claim prevents duplicates."""
        request = self.store.get_suggestion_request(str(request_id))
        if not request:
            raise ReviewConflict("suggestion request was not found")
        if request.get("status") != "queued":
            raise ReviewConflict("only a queued suggestion request can be resumed")
        return self.start_background(str(request_id))

    def _configured_proposer(self):
        if os.environ.get("RECIPE_IMPORTER_FIXTURE_MODE", "").strip().casefold() in {"1", "true", "yes", "on"}:
            raise RuntimeError("Fixture mode disables live suggestion requests")
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        from import_recipes import google_client, types
        from model_routing import ModelRouter, UsageLedger, parse_model_config
        from pydantic import BaseModel, Field
        from typing import Literal

        class ProposedInstruction(BaseModel):
            field_path: str = "instructions"
            target_id: str = ""
            proposed_value: str
            classification: Literal["inferred"] = "inferred"
            rationale: str
            uncertainty: str
            insertion_location: dict = Field(default_factory=lambda: {"field": "instructions", "after": None})

        class ProposalBatch(BaseModel):
            proposals: list[ProposedInstruction] = Field(default_factory=list)

        models = parse_model_config(os.environ.get("GEMINI_MODELS_JSON"), os.environ.get("GEMINI_MODEL", ""))
        self.router = self.router or ModelRouter(models, UsageLedger(self.output_dir / "gemini_usage.json"),
                                                 global_rpm=float(os.environ.get("GEMINI_GLOBAL_RPM", "12")))
        client = google_client(api_key)
        def run(snapshot: dict, kind: str):
            prompt = ("Suggest plausible missing recipe instructions as explicitly inferred, unverified content. "
                      "Do not claim source evidence. Return an empty proposals list when information is insufficient. "
                      "For a proposed correction to an existing instruction, echo that step's exact target_id. "
                      "For a genuinely additional step, use an explicit insertion_location. "
                      "Each proposal needs concise rationale and uncertainty.\nRECIPE JSON:\n" +
                      json.dumps(snapshot, ensure_ascii=False))
            output_budget = 512
            routed = self.router.run(lambda model: ProposalBatch.model_validate_json(
                client.models.generate_content(
                    model=model.id, contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json", response_schema=ProposalBatch,
                        max_output_tokens=output_budget),
                ).text or "{}"), retry_transient=False, stop_on_transient=True,
                request_tokens=(len(prompt.encode("utf-8")) + 3) // 4 + output_budget)
            self.router.last_model_id = routed.model_id
            return [item.model_dump(mode="json") for item in routed.value.proposals]
        return run


def _safe_error(exc: Exception) -> str:
    message = str(exc)
    for name in ("GEMINI_API_KEY", "MEALIE_TOKEN"):
        secret = os.environ.get(name, "")
        if secret:
            message = message.replace(secret, "[redacted]")
    return message[:300]
