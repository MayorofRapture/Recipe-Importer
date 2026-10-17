"""Small Mealie v3 client with separate recipe and tag operations."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable
from urllib.parse import quote

import requests

from recipe_quality import RecipeCandidate, schema_recipe
from runtime_safety import require_live_services


class MealieClient:
    def __init__(self, base_url: str, token: str, session: Any = None):
        if session is None:
            require_live_services()
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def get_recipe(self, slug: str) -> dict | None:
        """Read an explicitly selected slug; a 404 is not global absence proof."""
        if not slug.strip():
            raise ValueError("A recipe slug is required")
        response = self.session.get(self.base_url + "/api/recipes/" + quote(slug, safe=""),
                                    headers=self.headers, timeout=(10, 30))
        if response.status_code == 404:
            return None
        response.raise_for_status()
        recipe = response.json()
        if not isinstance(recipe, dict) or not recipe.get("id") or recipe.get("slug") != slug:
            raise ValueError("Mealie lookup did not return the selected recipe identity")
        return recipe

    def create_recipe(self, recipe: RecipeCandidate) -> str:
        response = self.session.post(
            self.base_url + "/api/recipes/create/html-or-json",
            json={"data": __import__("json").dumps(schema_recipe(recipe), ensure_ascii=False),
                  "includeTags": False, "includeCategories": False},
            headers=self.headers, timeout=(10, 100),
        )
        if response.status_code != 201:
            raise RuntimeError(f"Mealie HTTP {response.status_code}: {response.text[:400]}")
        slug = response.json()
        if not isinstance(slug, str) or not slug.strip():
            raise RuntimeError(f"Mealie returned an unexpected slug: {slug!r}")
        return slug

    def ensure_tag(self, name: str) -> dict:
        name = name.strip()
        if not name:
            raise ValueError("Batch tag must not be empty")
        existing = self._find_tag(name)
        if existing is not None:
            return existing
        return self._create_tag(name)

    def _find_tag(self, name: str) -> dict | None:
        response = self.session.get(self.base_url + "/api/organizers/tags?perPage=1000",
                                    headers=self.headers, timeout=(10, 30))
        response.raise_for_status()
        payload = response.json()
        items = payload.get("items", []) if isinstance(payload, dict) else payload
        for tag in items or []:
            if isinstance(tag, dict) and str(tag.get("name", "")).casefold() == name.casefold():
                return tag
        return None

    def _create_tag(self, name: str) -> dict:
        created = self.session.post(self.base_url + "/api/organizers/tags",
                                    json={"name": name}, headers=self.headers,
                                    timeout=(10, 30))
        if created.status_code in {200, 201}:
            tag = created.json()
            if isinstance(tag, dict):
                return tag
        if created.status_code == 409:
            existing = self._find_tag(name)
            if existing is not None:
                return existing
            raise RuntimeError("Mealie tag creation conflicted; retry tag repair after checking the tag list")
        raise RuntimeError(f"Mealie tag creation HTTP {created.status_code}: {created.text[:400]}")

    def add_tag(self, slug: str, tag: dict) -> str:
        response = self.session.get(self.base_url + "/api/recipes/" + slug,
                                    headers=self.headers, timeout=(10, 30))
        response.raise_for_status()
        recipe = deepcopy(response.json())
        existing = recipe.get("tags") or []
        if any(_same_tag(item, tag) for item in existing):
            return "already_present"
        recipe["tags"] = list(existing) + [tag]
        updated = self.session.put(self.base_url + "/api/recipes/" + slug,
                                   json=recipe, headers=self.headers, timeout=(10, 60))
        if updated.status_code not in {200, 201}:
            raise RuntimeError(f"Mealie tag update HTTP {updated.status_code}: {updated.text[:400]}")
        return "tagged"

    def backfill_tag(self, slugs: Iterable[str], tag_name: str, dry_run: bool = False) -> dict[str, str]:
        results: dict[str, str] = {}
        if dry_run:
            tag = self._find_tag(tag_name.strip())
            status = "would_tag" if tag is not None else "would_create_and_tag"
            return {slug: status for slug in slugs}
        tag = self.ensure_tag(tag_name)
        for slug in slugs:
            try:
                results[slug] = self.add_tag(slug, tag)
            except Exception:
                results[slug] = "failed"
        return results


def _same_tag(left: Any, right: dict) -> bool:
    if not isinstance(left, dict):
        return str(left).casefold() == str(right.get("name", "")).casefold()
    for key in ("id", "slug", "name"):
        if left.get(key) and right.get(key) and str(left[key]).casefold() == str(right[key]).casefold():
            return True
    return False
