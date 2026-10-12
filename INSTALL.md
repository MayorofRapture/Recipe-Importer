# Recipe-Importer Codex skills

Copy only the **three folders** inside `.agents/skills` into the same path inside your `Recipe-Importer` repository, keeping your installed Superpowers/Ponytail skills. `AGENTS.md` in this ZIP is a **replacement** for the previous repo-root file; it adds the new rule allowing **unverified Gemini suggestions** that must be human-approved or edited before use.

In PowerShell from your repository root, after extracting this ZIP elsewhere:

```powershell
Copy-Item -Recurse -Force "<EXTRACTED_FOLDER>\.agents\skills\*" ".\.agents\skills\"
Copy-Item -Force "<EXTRACTED_FOLDER>\AGENTS.md" ".\AGENTS.md"
Get-ChildItem .\.agents\skills -Directory | Select-Object -ExpandProperty Name
```

Then restart Codex if the skills do not appear. Review changes before committing:

```powershell
git status --short
git diff -- AGENTS.md
git add AGENTS.md .agents/skills/recipe-extraction-quality .agents/skills/import-safety .agents/skills/gemini-model-routing
git commit -m "Add project skills and reviewed recipe suggestion policy"
git push
```

These are **instructions for Codex**, not features added to `import_recipes.py`. The importer behavior will change only after Codex implements, tests, and you accept those changes.
