# Streamlit UI fixtures

The automated UI tests create sanitized temporary fixtures through
`tests/ui_fixture_factory.py`. They never read or modify the repository's
`output/` directory, source scans, `.env`, Gemini, or Mealie.
