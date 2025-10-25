# streamlit_app.py
import time
import streamlit as st
from app_core import run_core_app  # <-- main app logic lives here

st.set_page_config(page_title="🎧 Live Spotify Data", page_icon="🎧", layout="wide")

def hard_reset():
    # Clear all server-side caches + session and bump cache-busting params
    st.cache_data.clear()
    st.cache_resource.clear()
    st.session_state.clear()
    st.query_params.update({"v": str(int(time.time())), "sid": str(int(time.time()) % 100000), "mode": "core"})
    st.rerun()

mode = st.query_params.get("mode") or "safe"

if mode == "safe":
    # Minimal shell that always loads, even if the core app/session is stale
    st.title("🎧 Live Spotify Data — Lite Boot")
    st.caption("Loads a minimal shell first to avoid stale caches or stuck sessions.")

    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        if st.button("Enter App"):
            st.query_params.update({"mode": "core", "v": str(int(time.time()))})
            st.rerun()
    with col2:
        if st.button("Reset App (hard)"):
            hard_reset()
    with col3:
        st.link_button("Open Core (new tab)", url=f"?mode=core&v={int(time.time())}", type="secondary")

    with st.expander("If it hangs later…"):
        st.write(
            "Use **Reset App (hard)** to clear Streamlit caches + session and bump a cache-busting version, "
            "then reload the core app."
        )
    st.stop()

# ---- Core App guarded runner ----
try:
    run_core_app()
except Exception as e:
    st.error("Core app failed to start. Use **Reset App (hard)** and try again.")
    st.exception(e)
    if st.button("Reset App (hard)"):
        hard_reset()