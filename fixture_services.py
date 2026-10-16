"""Local fakes for explicitly marked synthetic browser libraries only."""
import json
import uuid
from pathlib import Path

from recipe_quality import schema_recipe
from review_store import _state_lock
from runtime_safety import fixture_mode


class FixtureMealie:
    def __init__(self, output_dir: Path):
        self.output = Path(output_dir)
        if not fixture_mode() or not (self.output / ".fixture-library").is_file():
            raise RuntimeError("Simulated services require fixture mode and a marked synthetic library")
        self.path = self.output / "fixture_mealie.json"

    def _load(self):
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"recipes": {}, "tags": {}}

    def _save(self, data):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def create_recipe(self, recipe):
        with _state_lock(self.path):
            data = self._load()
            slug = "simulation-" + uuid.uuid4().hex[:12]
            data["recipes"][slug] = {"id": uuid.uuid4().hex, "slug": slug, "name": recipe.name,
                                      "tags": [], "payload": schema_recipe(recipe)}
            self._save(data)
            return slug

    def get_recipe(self, slug):
        with _state_lock(self.path):
            return self._load()["recipes"].get(slug)

    def ensure_tag(self, name):
        with _state_lock(self.path):
            data = self._load()
            key = name.strip().casefold()
            tag = data["tags"].setdefault(key, {"id": uuid.uuid4().hex, "name": name.strip()})
            self._save(data)
            return tag

    def add_tag(self, slug, tag):
        with _state_lock(self.path):
            data = self._load()
            tags = data["recipes"][slug]["tags"]
            if any(item["id"] == tag["id"] for item in tags):
                return "already_present"
            tags.append(tag)
            self._save(data)
            return "tagged"
