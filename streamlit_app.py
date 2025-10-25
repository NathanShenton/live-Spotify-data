
# streamlit_app.py (Lite Boot + Core App)
import time
import streamlit as st

st.set_page_config(page_title="🎧 Live Spotify Data", page_icon="🎧", layout="wide")

def hard_reset():
    st.cache_data.clear()
    st.cache_resource.clear()
    st.session_state.clear()
    st.query_params.update({"v": str(int(time.time())), "sid": str(int(time.time()) % 100000), "mode": "core"})
    st.rerun()

mode = st.query_params.get("mode") or "safe"
if mode == "safe":
    st.title("🎧 Live Spotify Data — Lite Boot")
    st.caption("Loads a minimal shell first to avoid stale caches or stuck sessions.")
    col1, col2, col3 = st.columns([1,1,1])
    with col1:
        if st.button("Enter App"):
            st.query_params.update({"mode": "core", "v": str(int(time.time()))})
            st.rerun()
    with col2:
        if st.button("Reset App (hard)"):
            hard_reset()
    with col3:
        st.link_button("Open Core (new tab)", url="?mode=core&v="+str(int(time.time())), type="secondary")
    with st.expander("If it hangs later…"):
        st.write("Use **Reset App (hard)** to clear Streamlit caches + session and bump a cache-busting version, then reload the core app.")
    st.stop()

try:
    # CORE START
    
# ---- CORE APP (existing logic) ----
import os
import time
import json
import base64
import hashlib
from typing import Tuple, Optional, Dict
from urllib.parse import urlencode

import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

APP_VERSION = os.environ.get("APP_VERSION", None) or st.query_params.get("v") or "0"

def _rand_sid(n=6) -> str:
    import random, string
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))

def get_config() -> Dict[str, str]:
    try:
        client_id = st.secrets["SPOTIFY_CLIENT_ID"]
        client_secret = st.secrets["SPOTIFY_CLIENT_SECRET"]
        redirect_uri = st.secrets.get("SPOTIFY_REDIRECT_URI")
    except Exception:
        st.error("Missing Spotify secrets. Set SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI in .streamlit/secrets.toml")
        st.stop()

    qp_redirect = st.query_params.get("redirect_uri")
    if qp_redirect:
        redirect_uri = qp_redirect
    return {"CLIENT_ID": client_id, "CLIENT_SECRET": client_secret, "REDIRECT_URI": redirect_uri}

cfg = get_config()
CLIENT_ID = cfg["CLIENT_ID"]
CLIENT_SECRET = cfg["CLIENT_SECRET"]
REDIRECT_URI = cfg["REDIRECT_URI"]

SCOPES = "user-read-currently-playing user-read-playback-state user-read-recently-played"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_ME_PLAYER = "https://api.spotify.com/v1/me/player/currently-playing"
API_RECENTS = "https://api.spotify.com/v1/me/player/recently-played"
API_AUDIO_FEATURES = "https://api.spotify.com/v1/audio-features"
REQUEST_TIMEOUT = 8
MAX_RETRIES = 2

@st.cache_resource(show_spinner=False)
def _http(version_salt: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Accept": "application/json"})
    return s

def _b64_client_creds() -> str:
    return base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()

def _auth_link(state: str) -> str:
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "show_dialog": "false",
    }
    return f"{AUTH_URL}?{urlencode(params)}"

def _exchange_code_for_token(code: str) -> Dict:
    headers = {
        "Authorization": f"Basic {_b64_client_creds()}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI}
    resp = _http(APP_VERSION).post(TOKEN_URL, headers=headers, data=data, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    tok = resp.json()
    tok["expires_at"] = int(time.time()) + int(tok.get("expires_in", 3600)) - 30
    return tok

def _refresh_access_token(refresh_token: str) -> Dict:
    headers = {
        "Authorization": f"Basic {_b64_client_creds()}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    resp = _http(APP_VERSION).post(TOKEN_URL, headers=headers, data=data, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    tok = resp.json()
    if "refresh_token" not in tok:
        tok["refresh_token"] = refresh_token
    tok["expires_at"] = int(time.time()) + int(tok.get("expires_in", 3600)) - 30
    return tok

def ensure_token() -> Optional[Dict]:
    tok = st.session_state.get("spotify_token")
    if tok and int(time.time()) < tok.get("expires_at", 0):
        return tok
    if tok and tok.get("refresh_token"):
        try:
            new_tok = _refresh_access_token(tok["refresh_token"])
            st.session_state["spotify_token"] = new_tok
            return new_tok
        except Exception:
            st.warning("Token refresh failed. Please re-authenticate.")
            st.session_state.pop("spotify_token", None)
    code = st.query_params.get("code")
    if code:
        expected = st.session_state.get("oauth_state")
        returned = st.query_params.get("state")
        if expected and returned != expected:
            st.error("State mismatch. Please click login again.")
            return None
        try:
            tok = _exchange_code_for_token(code)
            st.session_state["spotify_token"] = tok
            v = st.query_params.get("v")
            st.query_params.clear()
            if v:
                st.query_params.update({"v": v, "mode": "core"})
            return tok
        except Exception as e:
            st.error(f"Token exchange failed: {e}")
            return None
    return None

def _auth_header(access_token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}

def _get_with_retry(url: str, headers: Dict[str, str], params: Dict = None) -> Tuple[int, Dict | None]:
    params = params or {}
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = _http(APP_VERSION).get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 204:
                return 204, None
            if resp.status_code == 200:
                return 200, resp.json()
            last_err = resp.text
        except Exception as e:
            last_err = str(e)
        time.sleep(0.25 * (attempt + 1))
    return 599, {"error": last_err or "Unknown error"}

@st.cache_data(ttl=120, show_spinner=False)
def get_audio_features_batch(access_token: str, track_ids: list[str], version_salt: str) -> Dict[str, Dict]:
    """Return dict id -> features for up to 100 IDs via /audio-features?ids=..."""
    result = {}
    ids = [tid for tid in track_ids if tid]
    if not ids:
        return result
    for i in range(0, len(ids), 100):
        chunk = ids[i:i+100]
        params = {"ids": ",".join(chunk)}
        status, payload = _get_with_retry(API_AUDIO_FEATURES, _auth_header(access_token), params=params)
        if status == 200 and isinstance(payload, dict):
            for f in payload.get("audio_features", []) or []:
                if f and f.get("id"):
                    result[f["id"]] = f
    return result

def feature_badges(feat: Dict) -> str:
    """Render simple HTML badges for a track's key audio features."""
    if not feat:
        return ""
    def pct(x):
        try: return int(round(float(x) * 100))
        except Exception: return 0
    tempo = int(round(feat.get("tempo", 0) or 0))
    energy = pct(feat.get("energy", 0))
    valence = pct(feat.get("valence", 0))
    dance = pct(feat.get("danceability", 0))
    html = f"""
    <div style='display:flex;gap:.5rem;flex-wrap:wrap;'>
      <span style='padding:.2rem .4rem;border-radius:.5rem;border:1px solid #ddd;'>🔥 Energy: {energy}%</span>
      <span style='padding:.2rem .4rem;border-radius:.5rem;border:1px solid #ddd;'>😊 Valence: {valence}%</span>
      <span style='padding:.2rem .4rem;border-radius:.5rem;border:1px solid #ddd;'>🕺 Dance: {dance}%</span>
      <span style='padding:.2rem .4rem;border-radius:.5rem;border:1px solid #ddd;'>⏱️ Tempo: {tempo} BPM</span>
    </div>"""
    return html

def ask_openai_about_track(api_key: str, model: str, track: Dict, artists: str) -> str:
    """Call OpenAI to get facts & meaning for a track."""
    if not api_key: return ""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    name = track.get("name", "Unknown Track")
    album = (track.get("album") or {}).get("name", "Unknown Album")
    rel = (track.get("album") or {}).get("release_date")
    prompt = f"""
You are a music expert. In 120-180 words, answer the following about the song below:

Song: {name}
Artist(s): {artists}
Album: {album}
Release date (if known): {rel}

Return in this structure:
- One interesting fact about the song
- One interesting fact about the artist
- What the song means
- One similar song and/or artist to try next (1–2 picks, explain why)
""".strip()
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Be accurate, concise, and use UK English."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
    }
    try:
        resp = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=20)
        if resp.status_code != 200:
            return f"OpenAI error: {resp.status_code} — {resp.text[:200]}"
        data = resp.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        return f"OpenAI request failed: {e}"

def display_now_playing(payload: Dict | None):
    if not payload:
        st.info("Nothing is currently playing.")
        return
    item = payload.get("item") or {}
    artists = ", ".join([a["name"] for a in item.get("artists", [])]) or "Unknown Artist"
    name = item.get("name", "Unknown Track")
    album = item.get("album", {}).get("name", "Unknown Album")
    art = (item.get("album", {}).get("images") or [{}])[0].get("url")
    with st.container(border=True):
        cols = st.columns([1, 3])
        with cols[0]:
            if art: st.image(art, use_container_width=True)
        with cols[1]:
            st.markdown(f"### 🎵 {name}")
            st.caption(f"{artists} — {album}")
            track_id = item.get("id")
            if track_id:
                feats = get_audio_features_batch(st.session_state["spotify_token"]["access_token"], [track_id], version_salt=APP_VERSION)
                f = feats.get(track_id)
                if f: st.markdown(feature_badges(f), unsafe_allow_html=True)
    with st.expander("🧠 AI Knowledge (optional)", expanded=False):
        st.caption("Paste your OpenAI API key locally (not stored).")
        if "openai_key" not in st.session_state: st.session_state["openai_key"] = ""
        st.session_state["openai_key"] = st.text_input("OpenAI API Key", type="password", value=st.session_state["openai_key"])
        model = st.selectbox("Model", ["gpt-4.1-mini", "gpt-4o-mini", "gpt-4.1"], index=0)
        custom_model = st.text_input("Custom model (optional)")
        chosen_model = custom_model.strip() or model
        if st.session_state.get("openai_key") and st.button("Get AI Knowledge for current song"):
            md = ask_openai_about_track(st.session_state["openai_key"], chosen_model, item, artists)
            st.markdown(md)

def display_recent(payload: Dict | None):
    if not payload: return
    items = payload.get("items", [])
    ids = [(it.get("track") or {}).get("id") for it in items]
    feats_map = get_audio_features_batch(st.session_state["spotify_token"]["access_token"], ids, version_salt=APP_VERSION)
    st.markdown("### ⏮️ Recently Played")
    for it in items:
        track = it.get("track") or {}
        name = track.get("name", "Unknown")
        artists = ", ".join([a["name"] for a in track.get("artists", [])])
        album = (track.get("album") or {}).get("name", "")
        art = (track.get("album", {}).get("images") or [{}])[0].get("url")
        with st.container(border=True):
            cols = st.columns([1, 3])
            with cols[0]:
                if art: st.image(art, use_container_width=True)
            with cols[1]:
                st.markdown(f"**{name}**")
                st.caption(f"{artists} — {album}")
        tid = track.get("id")
        if tid and tid in feats_map:
            st.markdown(feature_badges(feats_map[tid]), unsafe_allow_html=True)

def run_core_app():
    now = time.time()
    last = st.session_state.get("_last_active_ts", now)
    st.session_state["_last_active_ts"] = now
    if now - last > 20 * 60:
        st.info("Session was idle. If things seem stale, use **Reset App** in the sidebar.")

    st.title("🎧 Live Spotify Data")
    st.caption("Now Playing + Recent Plays with audio mood + optional AI Knowledge")

    with st.sidebar:
        limit = st.slider("Recent tracks to show", 1, 50, 10)
        auto_refresh = st.checkbox("Auto-refresh Now Playing (10s)", True)
        if st.button("Reset App (hard)"):
            st.cache_data.clear(); st.cache_resource.clear(); st.session_state.clear()
            st.query_params.update({"v": str(int(time.time())), "sid": _rand_sid(), "mode": "core"})
            st.rerun()

    if not REDIRECT_URI:
        st.error("Missing SPOTIFY_REDIRECT_URI in secrets."); st.stop()

    token = ensure_token()
    if not token:
        if "oauth_state" not in st.session_state:
            st.session_state["oauth_state"] = hashlib.sha256(os.urandom(32)).hexdigest()
        st.link_button("🔓 Login with Spotify", _auth_link(st.session_state["oauth_state"]), type="primary")
        st.stop()

    if auto_refresh:
        st_autorefresh(interval=10_000, key=f"nowplaying_refresh_{APP_VERSION}")

    access_token = token["access_token"]

    code_np, payload_np = _get_with_retry(API_ME_PLAYER, _auth_header(access_token))
    if code_np == 200:
        display_now_playing(payload_np)
    elif code_np == 204:
        st.info("Nothing currently playing.")
    else:
        st.error(f"Failed to fetch 'Now Playing' (status {code_np}).")

    code_rc, payload_rc = _get_with_retry(API_RECENTS, _auth_header(access_token), params={"limit": 10})
    if code_rc == 200:
        display_recent(payload_rc)
    else:
        st.error(f"Failed to fetch 'Recently Played' (status {code_rc}).")

    # CORE END
except Exception as e:
    st.error("Core app failed to start. Use **Reset App (hard)** and try again.")
    st.exception(e)
    if st.button("Reset App (hard)"):
        hard_reset()
    st.stop()
