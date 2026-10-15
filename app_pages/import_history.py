"""Read-only M1 destination for future import orchestration."""
import streamlit as st

from app_pages.common import catalog

st.title("Import & history")
st.caption("Import actions are intentionally unavailable in M1. This page shows local outcomes only.")
views = catalog()
if not views:
    st.info("No local recipe history is available yet.")
else:
    for status in ("Ready to import", "Imported", "Check Mealie", "Import failed", "Needs attention", "Ready to review"):
        matches = [view for view in views if view.status == status]
        if matches:
            st.subheader(status)
            st.write(", ".join(view.candidate.name for view in matches))
st.warning("Mealie is not contacted by this page. Import controls will be introduced in a later milestone.")
