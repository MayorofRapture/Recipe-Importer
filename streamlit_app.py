"""Recipe-Importer Streamlit entrypoint."""
import streamlit as st

st.set_page_config(page_title="Recipe-Importer", page_icon="🍲", layout="wide", initial_sidebar_state="expanded")

pages = {
    "Workflows": [
        st.Page("app_pages/upload.py", title="Upload", icon="📥"),
        st.Page("app_pages/review.py", title="Review", icon="📝", default=True),
        st.Page("app_pages/import_history.py", title="Import & history", icon="📚"),
    ]
}
navigation = st.navigation(pages)
navigation.run()
