"""st.Page objects by key, filled by app.py each run so pages can link to each other."""

PAGES = {}


def link(key, label, icon=None):
    import streamlit as st

    page = PAGES.get(key)
    if page is not None:
        st.page_link(page, label=label, icon=icon)
