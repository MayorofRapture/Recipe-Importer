"""Recipe-Importer Streamlit entrypoint."""
import streamlit as st
import os

st.set_page_config(page_title="Recipe-Importer", page_icon=":material/menu_book:", layout="wide")
from runtime_safety import (FixtureModeError, fixture_mode, fixture_output_dir,
                            validate_browser_fixture_environment)
from library_paths import LibraryPathConflict, alternate_cwd_library, resolve_library_path

fixture_library = None
browser_fixture_attestation = None
if fixture_mode():
    try:
        if os.environ.get("RECIPE_IMPORTER_BROWSER_FIXTURE") == "1":
            browser_fixture_attestation = validate_browser_fixture_environment()
        fixture_library = fixture_output_dir()
    except FixtureModeError as exc:
        st.error(str(exc), icon=":material/lock:")
        st.stop()

if fixture_mode():
    st.caption("Demonstration library · simulated services only")
if browser_fixture_attestation:
    st.caption(f"Isolated browser fixture: {browser_fixture_attestation}")

try:
    application_library = fixture_library or resolve_library_path()
except (FixtureModeError, LibraryPathConflict) as exc:
    st.error(str(exc), icon=":material/lock:")
    st.stop()
st.caption("Using the selected local library.")
alternate_library = alternate_cwd_library(application_library)
if alternate_library:
    st.warning("Another populated library exists. This app is using the selected library.")
with st.expander("Library troubleshooting"):
    st.caption(f"Selected library: {application_library}")
    if alternate_library:
        st.caption(f"Other populated library: {alternate_library}")
        st.caption("Set RECIPE_IMPORTER_OUTPUT_DIR to select the other library. Libraries are never merged or moved automatically.")

if "initial_destination" not in st.session_state:
    library = application_library
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
