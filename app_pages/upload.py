"""M1 Upload destination placeholder; extraction is intentionally not wired yet."""
import streamlit as st

st.title("Upload recipes")
st.caption("Cookbook uploads and extraction will be added after the Review workspace is proven.")
st.info("For now, review the existing extraction cache. No files are uploaded or sent to Gemini from this milestone.")
st.page_link("app_pages/review.py", label="Open Review")
