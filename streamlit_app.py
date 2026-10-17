"""Recipe-Importer Streamlit entrypoint."""
import streamlit as st
import os
from pathlib import Path

st.set_page_config(page_title="Recipe-Importer", page_icon=":material/menu_book:", layout="wide")
from runtime_safety import FixtureModeError, fixture_mode, fixture_output_dir

fixture_library = None
if fixture_mode():
    try:
        fixture_library = fixture_output_dir()
    except FixtureModeError as exc:
        st.error(str(exc), icon=":material/lock:")
        st.stop()

if fixture_mode():
    st.caption("Demonstration library · simulated services only")

if "initial_destination" not in st.session_state:
    library = fixture_library or Path(os.environ.get("RECIPE_IMPORTER_OUTPUT_DIR", "output"))
    st.session_state.initial_destination = (
        "Review" if any((library / "extracted").glob("*.json")) else
        "Import & history" if (library / "import_state.jsonl").exists() else "Upload"
    )

pages = [
        st.Page("app_pages/upload.py", title="Upload", icon=":material/upload:", default=True),
        st.Page("app_pages/review.py", title="Review", icon=":material/rate_review:", url_path="review"),
        st.Page("app_pages/import_history.py", title="Import & history", icon=":material/history:", url_path="import-history"),
]
navigation = st.navigation(pages, position="top")
from app_pages.common import guard_page_navigation

if "initial_redirect_done" not in st.session_state:
    st.session_state.initial_redirect_done = True
    if navigation.title.casefold() == "upload":
        if st.session_state.initial_destination == "Review":
            st.switch_page("app_pages/review.py")
        elif st.session_state.initial_destination == "Import & history":
            st.switch_page("app_pages/import_history.py")

if not guard_page_navigation(navigation.title.casefold()):
    st.switch_page("app_pages/review.py")
navigation.run()
