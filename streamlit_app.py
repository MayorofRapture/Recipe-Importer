"""Recipe-Importer Streamlit entrypoint."""
import streamlit as st

st.set_page_config(page_title="Recipe-Importer", page_icon=":material/menu_book:", layout="wide")

pages = {
    "Workflows": [
        st.Page("app_pages/upload.py", title="Upload", icon=":material/upload:"),
        st.Page("app_pages/review.py", title="Review", icon=":material/rate_review:", default=True),
        st.Page("app_pages/import_history.py", title="Import & history", icon=":material/history:"),
    ]
}
navigation = st.navigation(pages, position="top")
navigation.run()
