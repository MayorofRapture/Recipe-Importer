"""Shared legacy review projection; modern revisions remain ReviewStore authority."""
from dataclasses import dataclass

from recipe_quality import RecipeCandidate
from review_store import apply_candidate_decision, apply_review_decision


@dataclass(frozen=True)
class LegacyReview:
    recipe: RecipeCandidate
    approved: bool
    draft_conflict: bool


def project_legacy_review(candidate, decisions, candidate_decision, draft=None) -> LegacyReview:
    """Apply chronological decisions without letting a draft inherit older consent."""
    reviewed = candidate.model_copy(deep=True)
    conflict = False
    if draft is not None:
        try:
            reviewed = RecipeCandidate.model_validate(draft.recipe)
        except (ValueError, TypeError):
            conflict = True
        # Missing timestamps cannot establish that consent follows the draft.
        approved_after_draft = bool(candidate_decision and candidate_decision.timestamp
                                    and draft.saved_at
                                    and candidate_decision.timestamp >= draft.saved_at)
        conflict = conflict or not approved_after_draft
        decisions = [item for item in decisions if item.timestamp and draft.saved_at
                     and item.timestamp >= draft.saved_at]
        if not approved_after_draft:
            candidate_decision = None
    reviewed = apply_review_decision(reviewed, decisions)
    resolved_fields = {item.field_path for item in reviewed.suggestions
                       if item.review_status in {"approved", "edited"}}
    reviewed.missing_information = [item for item in reviewed.missing_information
                                    if item.field_path not in resolved_fields]
    reviewed = apply_candidate_decision(reviewed, candidate_decision)
    if conflict:
        reviewed.requires_review = True
    approved = bool(candidate_decision and candidate_decision.status in {"approved", "edited"}
                    and not conflict)
    return LegacyReview(reviewed, approved, conflict)
