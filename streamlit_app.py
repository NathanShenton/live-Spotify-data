# app.py
import os
import time
import json
import base64
import hashlib
from typing import Tuple, Optional, Dict
from urllib.parse import urlencode

import requests
import streamlit as st

st.set_page_config(page_title="Live Spotify Data", page_icon="🎧", layout="wide")

# ==============================
# Versioning & Cache Busting
# ==============================
# Use env var if provided (Streamlit Cloud: set in Secrets) or query param ?v= to salt caches.
APP_VERSION = os.environ.get("APP_VERSION", None) or st.query_params.get("v") or "0"

def _rand_sid(n=6) -> str:
    import random, string
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))

# ==============================
# Config fetch (deferred; no secrets at import)
# ==============================
def get_config() -> Dict[str, str]:
    try:
        client_id = st.secrets["SPOTIFY_CLIENT_ID"]
        client_secret = st.secrets["SPOTIFY_CLIENT_SECRET"]
        redirect_uri = st.secrets.get("SPOTIFY_REDIRECT_URI")
    except Exception as e:
        st.error("Missing Spotify secrets. Please set SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI in .streamlit/secrets.toml")
        st.stop()
    # Allow override via query param for local testing only
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

REQUEST_TIMEOUT = 8
MAX_RETRIES = 2

# ==============================
# HTTP session (salted by version to nuke across deployments)
# ==============================
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
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
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
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
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
        except Exception as e:
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
            # clear URL params but preserve version
            v = st.query_params.get("v")
            st.query_params.clear()
            if v:
                st.query_params.update({"v": v})
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
            if resp.status_code == 401 and st.session_state.get("spotify_token", {}).get("refresh_token"):
                try:
                    new_tok = _refresh_access_token(st.session_state["spotify_token"]["refresh_token"])
                    st.session_state["spotify_token"] = new_tok
                    headers = _auth_header(new_tok["access_token"])
                    continue
                except Exception as re:
                    return 401, {"error": f"Unauthorized and refresh failed: {re}"}
            if resp.status_code >= 500:
                last_err = resp.text
                time.sleep(0.4 * (attempt + 1))
                continue
            if resp.status_code == 204:
                return 204, None
            if resp.status_code != 200:
                return resp.status_code, {"error": resp.text}
            return 200, resp.json()
        except requests.exceptions.RequestException as e:
            last_err = str(e)
            time.sleep(0.2 * (attempt + 1))
            continue
    return 599, {"error": last_err or "Unknown network error"}

def get_currently_playing(access_token: str) -> Tuple[int, Dict | None]:
    return _get_with_retry(API_ME_PLAYER, _auth_header(access_token))

@st.cache_data(ttl=60, show_spinner=False)
def get_recent_tracks_cached(access_token: str, limit: int, version_salt: str) -> Tuple[int, Dict | None]:
    params = {"limit": max(1, min(limit, 50))}
    return _get_with_retry(API_RECENTS, _auth_header(access_token), params=params)

def display_now_playing(payload: Dict | None):
    if not payload:
        st.info("Nothing is currently playing on your account.")
        return
    is_playing = payload.get("is_playing", False)
    item = payload.get("item") or {}
    artists = ", ".join([a["name"] for a in item.get("artists", [])]) or "Unknown Artist"
    name = item.get("name", "Unknown Track")
    album = item.get("album", {}).get("name", "Unknown Album")
    art = None
    images = (item.get("album") or {}).get("images") or []
    if images:
        art = images[0].get("url")
    with st.container(border=True):
        cols = st.columns([1, 3])
        with cols[0]:
            if art:
                st.image(art, use_container_width=True)
        with cols[1]:
            st.markdown("### 🎵 Now Playing" + (" • LIVE" if is_playing else " • Paused"))
            st.markdown(f"**{name}**")
            st.caption(f"{artists} — {album}")
            progress = payload.get("progress_ms", 0) / 1000.0
            duration = (item.get("duration_ms", 0) or 0) / 1000.0
            if duration > 0:
                st.progress(min(1.0, progress / duration))

def display_recent(payload: Dict | None):
    if not payload:
        return
    items = payload.get("items", [])
    st.markdown("### ⏮️ Recently Played")
    for it in items:
        track = (it.get("track") or {})
        played_at = it.get("played_at", "")
        name = track.get("name", "Unknown Track")
        artists = ", ".join([a["name"] for a in track.get("artists", [])]) or "Unknown Artist"
        album = (track.get("album") or {}).get("name", "Unknown Album")
        images = (track.get("album") or {}).get("images") or []
        art = images[2]["url"] if len(images) > 2 else (images[0]["url"] if images else None)
        with st.container(border=True):
            cols = st.columns([1, 3, 2])
            with cols[0]:
                if art:
                    st.image(art, use_container_width=True)
            with cols[1]:
                st.markdown(f"**{name}**")
                st.caption(f"{artists} — {album}")
            with cols[2]:
                st.caption(played_at.replace("T", " ").replace("Z", " UTC"))

# ==============================
# UI
# ==============================
st.title("🎧 Live Spotify Data")
st.caption("Minimal demo: Now Playing + Recent Plays (secure secrets via Streamlit)")

left, right = st.columns([3, 1])
with right:
    # Sidebar-like controls inline to avoid rerender issues on some hosts
    limit = st.slider("Recent tracks to show", min_value=1, max_value=50, value=10)
    auto_refresh = st.checkbox("Auto-refresh Now Playing (10s)", value=True)
    if st.button("Force clean session"):
        # Clear Streamlit caches and session, and bump v to break service-worker cache
        st.cache_data.clear()
        st.cache_resource.clear()
        st.session_state.clear()
        st.query_params.update({"v": str(int(time.time())), "sid": _rand_sid()})
        st.rerun()

with st.expander("🔐 Secrets status (local-only)", expanded=False):
    ok_vars = {
        "SPOTIFY_CLIENT_ID": bool(CLIENT_ID),
        "SPOTIFY_CLIENT_SECRET": bool(CLIENT_SECRET),
        "SPOTIFY_REDIRECT_URI": bool(REDIRECT_URI),
        "APP_VERSION": APP_VERSION,
    }
    st.json(ok_vars)

if not REDIRECT_URI:
    st.error("Missing SPOTIFY_REDIRECT_URI. Set it in .streamlit/secrets.toml (must exactly match your Spotify app Redirect URI).")
    st.stop()

# Auth: ensure token BEFORE enabling refresh
token = ensure_token()

if not token:
    if "oauth_state" not in st.session_state:
        st.session_state["oauth_state"] = hashlib.sha256(os.urandom(32)).hexdigest()
    auth_link = _auth_link(st.session_state["oauth_state"])
    st.warning("You need to login with Spotify to continue.")
    st.link_button("🔓 Login with Spotify", auth_link, type="primary")
    st.stop()

# Same-session rerun only (no full page reload)
if auto_refresh:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=10_000, key=f"nowplaying_refresh_{APP_VERSION}")

access_token = token["access_token"]

# Now Playing
status_np, payload_np = get_currently_playing(access_token)
if status_np == 200:
    display_now_playing(payload_np)
elif status_np == 204:
    st.info("Nothing currently playing.")
else:
    st.error(f"Failed to fetch 'Now Playing' (status {status_np}).")
    if isinstance(payload_np, dict) and "error" in payload_np:
        st.code(payload_np["error"])

# Recent (cached; salted by APP_VERSION so new deploys bust old cache)
status_rc, payload_rc = get_recent_tracks_cached(access_token, limit=limit, version_salt=APP_VERSION)
if status_rc == 200:
    display_recent(payload_rc)
else:
    st.error(f"Failed to fetch 'Recently Played' (status {status_rc}).")
    if isinstance(payload_rc, dict) and "error" in payload_rc:
        st.code(payload_rc["error"])
