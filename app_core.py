# app_core.py
import os
import time
import base64
import hashlib
from typing import Tuple, Optional, Dict, List
from urllib.parse import urlencode

import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

# --------------------------
# Versioning & cache busting
# --------------------------
APP_VERSION = os.environ.get("APP_VERSION", None) or st.query_params.get("v") or "0"

# --------------------------
# Constants
# --------------------------
SCOPES = "user-read-currently-playing user-read-playback-state user-read-recently-played"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_ME_PLAYER = "https://api.spotify.com/v1/me/player/currently-playing"
API_RECENTS = "https://api.spotify.com/v1/me/player/recently-played"
API_AUDIO_FEATURES = "https://api.spotify.com/v1/audio-features"

REQUEST_TIMEOUT = 8
MAX_RETRIES = 2

# --------------------------
# Helpers
# --------------------------
def _rand_sid(n: int = 6) -> str:
    import random, string
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))

def _error_stop(msg: str) -> None:
    st.error(msg)
    st.stop()

def _get_config() -> Dict[str, str]:
    # Defer secrets access to runtime so the shell can always render
    try:
        client_id = st.secrets["SPOTIFY_CLIENT_ID"]
        client_secret = st.secrets["SPOTIFY_CLIENT_SECRET"]
        redirect_uri = st.secrets.get("SPOTIFY_REDIRECT_URI")
    except Exception:
        _error_stop("Missing Spotify secrets. Set SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI in .streamlit/secrets.toml")
    qp_redirect = st.query_params.get("redirect_uri")
    if qp_redirect:
        redirect_uri = qp_redirect
    return {"CLIENT_ID": client_id, "CLIENT_SECRET": client_secret, "REDIRECT_URI": redirect_uri}

@st.cache_resource(show_spinner=False)
def _http(version_salt: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Accept": "application/json"})
    return s

def _b64_client_creds(client_id: str, client_secret: str) -> str:
    return base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

def _auth_link(client_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "show_dialog": "false",
    }
    return f"{AUTH_URL}?{urlencode(params)}"

def _exchange_code_for_token(client_id: str, client_secret: str, redirect_uri: str, code: str) -> Dict:
    headers = {
        "Authorization": f"Basic {_b64_client_creds(client_id, client_secret)}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
    resp = _http(APP_VERSION).post(TOKEN_URL, headers=headers, data=data, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    tok = resp.json()
    tok["expires_at"] = int(time.time()) + int(tok.get("expires_in", 3600)) - 30
    return tok

def _refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> Dict:
    headers = {
        "Authorization": f"Basic {_b64_client_creds(client_id, client_secret)}",
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

def ensure_token(cfg: Dict[str, str]) -> Optional[Dict]:
    tok = st.session_state.get("spotify_token")
    # still valid?
    if tok and int(time.time()) < tok.get("expires_at", 0):
        return tok
    # try refresh
    if tok and tok.get("refresh_token"):
        try:
            new_tok = _refresh_access_token(cfg["CLIENT_ID"], cfg["CLIENT_SECRET"], tok["refresh_token"])
            st.session_state["spotify_token"] = new_tok
            return new_tok
        except Exception:
            st.warning("Token refresh failed. Please re-authenticate.")
            st.session_state.pop("spotify_token", None)

    # try auth code
    code = st.query_params.get("code")
    if code:
        expected = st.session_state.get("oauth_state")
        returned = st.query_params.get("state")
        if expected and returned != expected:
            _error_stop("State mismatch. Please click login again.")
        try:
            tok = _exchange_code_for_token(cfg["CLIENT_ID"], cfg["CLIENT_SECRET"], cfg["REDIRECT_URI"], code)
            st.session_state["spotify_token"] = tok
            v = st.query_params.get("v")
            st.query_params.clear()
            if v:
                st.query_params.update({"v": v, "mode": "core"})
            return tok
        except Exception as e:
            _error_stop(f"Token exchange failed: {e}")
    return None

def _auth_header(access_token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}

def _get_with_retry(url: str, headers: Dict[str, str], params: Dict = None) -> Tuple[int, Dict | None]:
    """
    GET with small retry budget + 401 refresh once.
    Returns: (status_code, payload or {"error": ...} or None for 204)
    """
    params = params or {}
    last_err = None
    did_refresh = False
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = _http(APP_VERSION).get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            # No content
            if resp.status_code == 204:
                return 204, None
            # OK
            if resp.status_code == 200:
                return 200, resp.json()
            # Unauthorized → try refresh once if we can
            if resp.status_code == 401 and not did_refresh:
                tok = st.session_state.get("spotify_token") or {}
                rtok = tok.get("refresh_token")
                cfg = _get_config()
                if rtok:
                    try:
                        new_tok = _refresh_access_token(cfg["CLIENT_ID"], cfg["CLIENT_SECRET"], rtok)
                        st.session_state["spotify_token"] = new_tok
                        headers = _auth_header(new_tok["access_token"])
                        did_refresh = True
                        continue  # retry immediately
                    except Exception as e:
                        return 401, {"error": f"Unauthorized and refresh failed: {e}"}
                return 401, {"error": "Unauthorized"}
            # Server/transient → small backoff then retry
            if resp.status_code >= 500:
                last_err = resp.text
                time.sleep(0.25 * (attempt + 1))
                continue
            # Other client errors
            return resp.status_code, {"error": resp.text}
        except Exception as e:
            last_err = str(e)
            time.sleep(0.2 * (attempt + 1))
            continue
    return 599, {"error": last_err or "Unknown error"}

# --------------------------
# Formatting helpers
# --------------------------
def _fmt_ms(ms: int) -> str:
    """Convert milliseconds to M:SS, floor seconds."""
    if not ms or ms < 0:
        return "0:00"
    s = int(ms // 1000)
    m, s = divmod(s, 60)
    return f"{m}:{s:02d}"

def _meta_chips(item: Dict) -> str:
    """Render compact chips for key track metadata."""
    album = item.get("album") or {}
    release = album.get("release_date") or ""
    year = release[:4] if release else ""
    popularity = item.get("popularity")
    explicit = item.get("explicit", False)
    track_no = item.get("track_number")
    disc_no = item.get("disc_number")

    chips = []
    if year:
        chips.append(f"📆 {year}")
    if popularity is not None:
        chips.append(f"⭐ {popularity}")
    chips.append("⚠️ Explicit" if explicit else "🟢 Clean")
    if track_no:
        chips.append(f"🔢 Track {track_no}")
    if disc_no and disc_no > 1:
        chips.append(f"💿 Disc {disc_no}")

    html = "<div style='display:flex;gap:.5rem;flex-wrap:wrap;'>"
    for c in chips:
        html += f"<span style='padding:.2rem .4rem;border-radius:.5rem;border:1px solid #ddd;'>{c}</span>"
    html += "</div>"
    return html

# ------------
# Audio feats
# ------------
@st.cache_data(ttl=120, show_spinner=False)
def get_audio_features_batch(access_token: str, track_ids: List[str], version_salt: str) -> Dict[str, Dict]:
    """
    Return dict id -> features for up to 100 IDs via /audio-features?ids=...
    - Skips empty/duplicate IDs
    - Tolerates None entries in Spotify's response list
    """
    result: Dict[str, Dict] = {}
    # Clean IDs
    ids = [tid for tid in (track_ids or []) if tid]
    if not ids:
        return result
    # De-dup to avoid bloating cache/API
    seen = set()
    ids = [i for i in ids if not (i in seen or seen.add(i))]
    # Chunk by 100
    for i in range(0, len(ids), 100):
        chunk = ids[i:i+100]
        params = {"ids": ",".join(chunk)}
        code, payload = _get_with_retry(API_AUDIO_FEATURES, _auth_header(access_token), params=params)
        if code == 200 and isinstance(payload, dict):
            for f in payload.get("audio_features", []) or []:
                if f and f.get("id"):
                    result[f["id"]] = f
        # If non-200, just skip this chunk (avoid breaking UI)
    return result

def feature_badges(feat: Dict) -> str:
    """Render simple HTML badges for key audio features."""
    if not feat:
        return ""
    def pct(x):
        try:
            return int(round(float(x) * 100))
        except Exception:
            return 0
    tempo = int(round(feat.get("tempo", 0) or 0))
    energy = pct(feat.get("energy", 0))
    valence = pct(feat.get("valence", 0))  # "happiness"
    dance = pct(feat.get("danceability", 0))
    html = f"""
    <div style='display:flex;gap:.5rem;flex-wrap:wrap;'>
      <span style='padding:.2rem .4rem;border-radius:.5rem;border:1px solid #ddd;'>🔥 Energy: {energy}%</span>
      <span style='padding:.2rem .4rem;border-radius:.5rem;border:1px solid #ddd;'>😊 Valence: {valence}%</span>
      <span style='padding:.2rem .4rem;border-radius:.5rem;border:1px solid #ddd;'>🕺 Dance: {dance}%</span>
      <span style='padding:.2rem .4rem;border-radius:.5rem;border:1px solid #ddd;'>⏱️ Tempo: {tempo} BPM</span>
    </div>
    """
    return html

# ------------
# OpenAI panel
# ------------
def ask_openai_about_track(api_key: str, model: str, track: Dict, artists: str) -> str:
    """Call OpenAI to get facts/meaning/recs for a track."""
    if not api_key:
        return ""
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
        return data.get("choices", [{}])[0].get("message", {}).get("content", "") or "No response."
    except Exception as e:
        return f"OpenAI request failed: {e}"

# --------------------------
# UI helpers
# --------------------------

def _now_playing(access_token: str):
    code, payload = _get_with_retry(API_ME_PLAYER, _auth_header(access_token))

    if code == 204:
        st.info("Nothing is currently playing.")
        return

    if code != 200:
        st.error(f"Failed to fetch 'Now Playing' (status {code}).")
        return

    item = (payload or {}).get("item") or {}
    is_playing = bool((payload or {}).get("is_playing"))
    progress_ms = int((payload or {}).get("progress_ms") or 0)
    duration_ms = int(item.get("duration_ms") or 0)

    artists_txt = ", ".join([a["name"] for a in item.get("artists", [])])
    name = item.get("name", "Unknown Track")
    album = (item.get("album") or {}).get("name", "Unknown Album")
    art = (item.get("album", {}).get("images") or [{}])[0].get("url")

    show_ai = st.session_state.get("show_ai", False)

    if show_ai:
        left, centre, right = st.columns([1, 3, 2])
    else:
        left, centre = st.columns([1, 5])

    with left:
        if art:
            st.image(art, width=120)

    with centre:
        st.markdown(f"### 🎵 {name}")
        st.caption(f"{artists_txt} • {album}")
        st.write("🟢 Playing" if is_playing else "⏸️ Paused")

        if duration_ms > 0:
            st.progress(max(0.0, min(1.0, progress_ms / duration_ms)))
            st.caption(f"{_fmt_ms(progress_ms)} / {_fmt_ms(duration_ms)}")

        st.markdown(_meta_chips(item), unsafe_allow_html=True)

    if show_ai:
        with right:
            st.markdown("#### 🧠 AI Insight")
            if "openai_key" not in st.session_state:
                st.session_state["openai_key"] = ""

            st.session_state["openai_key"] = st.text_input(
                "OpenAI API Key",
                type="password",
                value=st.session_state["openai_key"]
            )

    st.divider()


def _recent(access_token: str, limit: int):
    code_rc, payload_rc = _get_with_retry(
        API_RECENTS,
        _auth_header(access_token),
        params={"limit": max(1, min(limit, 50))}
    )

    if code_rc != 200:
        st.error("Failed to fetch recent tracks.")
        return

    items = payload_rc.get("items", []) or []

    st.markdown("### ⏮️ Recently Played")

    for it in items:
        tr = it.get("track") or {}

        name = tr.get("name", "Unknown")
        artists = ", ".join([a["name"] for a in tr.get("artists", [])])

        art = (tr.get("album", {}).get("images") or [{}])[0].get("url")

        img_col, txt_col = st.columns([0.35, 5])

        with img_col:
            if art:
                st.image(art, width=55)

        with txt_col:
            st.markdown(f"**{name}**")
            st.caption(artists)

        st.divider()

# ------------
# Main entry
# ------------
def run_core_app():
    cfg = _get_config()
    if not cfg["REDIRECT_URI"]:
        _error_stop("Missing SPOTIFY_REDIRECT_URI in secrets.")

    # Title & controls
    st.title("🎧 Live Spotify Data")
    st.caption("Now Playing + Recent Plays with audio mood + optional AI Knowledge")

    st.markdown("""
    <style>
    .main .block-container {max-width:1500px;padding-top:1rem;}
    [data-testid="stImage"] img {border-radius:8px;}
    </style>
    """, unsafe_allow_html=True)

    with st.sidebar:
        limit = st.slider("Recent tracks to show", 5, 50, 10)\n        show_ai = st.checkbox("Show AI Insights", value=False)\n        st.session_state["show_ai"] = show_ai
        auto_refresh = st.checkbox("Auto-refresh Now Playing (10s)", True)
        if st.button("Reset App (hard)"):
            st.cache_data.clear()
            st.cache_resource.clear()
            st.session_state.clear()
            st.query_params.update({"v": str(int(time.time())), "sid": _rand_sid(), "mode": "core"})
            st.rerun()

    # Idle watchdog nudge
    now = time.time()
    last = st.session_state.get("_last_active_ts", now)
    st.session_state["_last_active_ts"] = now
    if now - last > 20 * 60:
        st.info("Session was idle. If things seem stale, use **Reset App (hard)** in the sidebar.")

    # OAuth ensure token
    token = ensure_token(cfg)
    if not token:
        if "oauth_state" not in st.session_state:
            st.session_state["oauth_state"] = hashlib.sha256(os.urandom(32)).hexdigest()
        st.link_button("🔓 Login with Spotify", _auth_link(cfg["CLIENT_ID"], cfg["REDIRECT_URI"], st.session_state["oauth_state"]), type="primary")
        st.stop()

    if auto_refresh:
        # Session-safe rerun; does not destroy session state (unlike window reload)
        st_autorefresh(interval=10_000, key=f"nowplaying_refresh_{APP_VERSION}")

    access_token = token["access_token"]

    # Now Playing + Recent
    _now_playing(access_token)
    _recent(access_token, limit)
