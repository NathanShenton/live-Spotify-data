# streamlit_app.py
import os
import time
import json
import base64
import hashlib
import requests
import streamlit as st
from urllib.parse import urlencode

st.set_page_config(page_title="Live Spotify Data", page_icon="🎧", layout="wide")

# --- Configuration (from Streamlit Secrets) ---
CLIENT_ID = st.secrets["SPOTIFY_CLIENT_ID"]
CLIENT_SECRET = st.secrets["SPOTIFY_CLIENT_SECRET"]
REDIRECT_URI = st.secrets.get("SPOTIFY_REDIRECT_URI", st.experimental_get_query_params().get("redirect_uri", [""])[0])
# Recommended scopes for now playing + recents
SCOPES = "user-read-currently-playing user-read-playback-state user-read-recently-played"

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_ME_PLAYER = "https://api.spotify.com/v1/me/player/currently-playing"
API_RECENTS = "https://api.spotify.com/v1/me/player/recently-played"

# --- Helpers ---
def b64_client_creds(client_id: str, client_secret: str) -> str:
    creds = f"{client_id}:{client_secret}".encode("utf-8")
    return base64.b64encode(creds).decode("utf-8")

def get_auth_url(state: str) -> str:
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "show_dialog": "false",
    }
    return f"{AUTH_URL}?{urlencode(params)}"

def exchange_code_for_token(code: str) -> dict:
    headers = {
        "Authorization": f"Basic {b64_client_creds(CLIENT_ID, CLIENT_SECRET)}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    resp = requests.post(TOKEN_URL, headers=headers, data=data, timeout=20)
    if resp.status_code != 200:
        st.error(f"Token exchange failed: {resp.status_code} {resp.text}")
        return {}
    tok = resp.json()
    # Persist token + expiry
    tok["expires_at"] = int(time.time()) + int(tok.get("expires_in", 3600)) - 30  # 30s skew
    return tok

def refresh_access_token(refresh_token: str) -> dict:
    headers = {
        "Authorization": f"Basic {b64_client_creds(CLIENT_ID, CLIENT_SECRET)}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    resp = requests.post(TOKEN_URL, headers=headers, data=data, timeout=20)
    if resp.status_code != 200:
        st.warning(f"Refresh failed: {resp.status_code} {resp.text}")
        return {}
    tok = resp.json()
    if "refresh_token" not in tok:
        tok["refresh_token"] = refresh_token  # keep the old one per Spotify docs
    tok["expires_at"] = int(time.time()) + int(tok.get("expires_in", 3600)) - 30
    return tok

def ensure_token() -> dict | None:
    tok = st.session_state.get("spotify_token")
    if tok and int(time.time()) < tok.get("expires_at", 0):
        return tok
    if tok and tok.get("refresh_token"):
        new_tok = refresh_access_token(tok["refresh_token"])
        if new_tok:
            st.session_state["spotify_token"] = new_tok
            return new_tok
    # Try code in query params
    q = st.experimental_get_query_params()
    code = q.get("code", [None])[0]
    if code:
        state_returned = q.get("state", [None])[0]
        expected = st.session_state.get("oauth_state")
        if expected and state_returned != expected:
            st.error("State mismatch. Please try logging in again.")
            return None
        tok = exchange_code_for_token(code)
        if tok:
            st.session_state["spotify_token"] = tok
            # Clean the URL (remove code/state)
            st.experimental_set_query_params()
            return tok
    return None

def auth_header(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}

def get_currently_playing(access_token: str) -> tuple[int, dict | None]:
    resp = requests.get(API_ME_PLAYER, headers=auth_header(access_token), timeout=20)
    if resp.status_code == 204:
        return (204, None)  # No content
    if resp.status_code != 200:
        return (resp.status_code, {"error": resp.text})
    return (200, resp.json())

def get_recent_tracks(access_token: str, limit: int = 10) -> tuple[int, dict | None]:
    params = {"limit": max(1, min(limit, 50))}
    resp = requests.get(API_RECENTS, headers=auth_header(access_token), params=params, timeout=20)
    if resp.status_code != 200:
        return (resp.status_code, {"error": resp.text})
    return (200, resp.json())

def display_now_playing(payload: dict | None):
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

def display_recent(payload: dict | None):
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

# --- UI ---
st.title("🎧 Live Spotify Data")
st.caption("Minimal demo: Now Playing + Recent Plays (secure secrets via Streamlit)")

# Ask for recent limit (1–50)
limit = st.sidebar.slider("Recent tracks to show", min_value=1, max_value=50, value=10)

# Show secret health
with st.expander("🔐 Secrets status (local-only)", expanded=False):
    ok_vars = {
        "SPOTIFY_CLIENT_ID": bool(CLIENT_ID),
        "SPOTIFY_CLIENT_SECRET": bool(CLIENT_SECRET),
        "SPOTIFY_REDIRECT_URI": bool(REDIRECT_URI),
    }
    st.json(ok_vars)

# Ensure we know our redirect_uri
if not REDIRECT_URI:
    st.error("Missing SPOTIFY_REDIRECT_URI in secrets. Set it to your deployed app URL (exact match).")
    st.stop()

# Try to ensure token
token = ensure_token()

if not token:
    # Kick off OAuth
    if "oauth_state" not in st.session_state:
        st.session_state["oauth_state"] = hashlib.sha256(os.urandom(32)).hexdigest()
    auth_link = get_auth_url(st.session_state["oauth_state"])
    st.warning("You need to login with Spotify to continue.")
    st.link_button("🔓 Login with Spotify", auth_link, type="primary")
    st.stop()

access_token = token["access_token"]

# Fetch Now Playing
status_np, payload_np = get_currently_playing(access_token)
if status_np == 200:
    display_now_playing(payload_np)
elif status_np == 204:
    st.info("Nothing currently playing.")
else:
    st.error(f"Failed to fetch 'Now Playing': {payload_np.get('error', '')}")

# Fetch Recent
status_rc, payload_rc = get_recent_tracks(access_token, limit=limit)
if status_rc == 200:
    display_recent(payload_rc)
else:
    st.error(f"Failed to fetch 'Recently Played': {payload_rc.get('error', '')}")