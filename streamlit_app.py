# app.py — Spotify Now Playing + Mood (PKCE, sticky-state, hardened)

from typing import Optional
import os, json, base64, hashlib, secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
import streamlit as st

# ---------- Fast boot knobs ----------
REQUEST_TIMEOUT = 8   # seconds
IMG_INDEX = -1        # smallest album image

st.set_page_config(page_title="Spotify Now + Mood (PKCE)", page_icon="🎧", layout="wide")

SCOPES = [
    "user-read-playback-state",
    "user-read-currently-playing",
    "user-read-recently-played",
]

# ---------- Session init ----------
def _init_state():
    ss = st.session_state
    ss.setdefault("spotify_client_id", "")
    ss.setdefault("redirect_uri", "")        # exact app URL, no trailing slash unless added in Spotify
    ss.setdefault("openai_api_key", "")

    # OAuth/PKCE
    ss.setdefault("pkce_code_verifier", None)
    ss.setdefault("oauth_state_csrf", None)
    ss.setdefault("last_auth_url", None)
    ss.setdefault("auth_error", "")

    # Tokens
    ss.setdefault("access_token", None)
    ss.setdefault("refresh_token", None)
    ss.setdefault("expires_at", None)
    ss.setdefault("authed", False)

    # App caches
    ss.setdefault("recent_cache", None)
    ss.setdefault("mood_json", None)

_init_state()

# ---------- Helpers ----------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def token_expired() -> bool:
    exp = st.session_state.expires_at
    return (not exp) or (now_utc() >= exp)

def b64url_encode_bytes(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")

def b64url_decode_str(s: str) -> bytes:
    pad = '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)

def gen_code_verifier() -> str:
    return b64url_encode_bytes(os.urandom(64))  # ~86 chars, valid (43..128)

def code_challenge_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return b64url_encode_bytes(digest)

def new_csrf_token() -> str:
    return secrets.token_urlsafe(16)

def read_qp_single(name: str) -> Optional[str]:
    try:
        qp = st.query_params  # type: ignore[attr-defined]
        if name in qp:
            v = qp[name]
            return v[0] if isinstance(v, list) else v
    except Exception:
        pass
    try:
        qp = st.experimental_get_query_params()
        if name in qp:
            v = qp[name]
            return v[0] if isinstance(v, list) else v
    except Exception:
        pass
    return None

def ms_fmt(ms):
    if ms is None: return "-"
    s = ms // 1000
    return f"{s//60}:{s%60:02d}"

# ---------- OAuth (PKCE) with sticky state ----------
def build_auth_url() -> Optional[str]:
    cid = st.session_state.spotify_client_id.strip()
    redir = st.session_state.redirect_uri.strip()
    if not cid or not redir:
        return None

    verifier = gen_code_verifier()
    challenge = code_challenge_s256(verifier)
    csrf = new_csrf_token()

    # Save in-session (best case)
    st.session_state.pkce_code_verifier = verifier
    st.session_state.oauth_state_csrf = csrf

    # Sticky payload (to rebuild after redirect/new session)
    payload = {"v": verifier, "cid": cid, "ru": redir}
    payload_b64 = b64url_encode_bytes(json.dumps(payload).encode("utf-8"))

    # state: "<csrf>.<payload_b64>"
    state_full = f"{csrf}.{payload_b64}"

    q = {
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": redir,
        "scope": " ".join(SCOPES),
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state_full,
        "show_dialog": "false",
    }
    return f"https://accounts.spotify.com/authorize?{urlencode(q)}"

def exchange_code_for_token(code: str) -> bool:
    try:
        cid = st.session_state.spotify_client_id.strip()
        redir = st.session_state.redirect_uri.strip()
        verifier = st.session_state.pkce_code_verifier
        if not (cid and redir and verifier):
            st.session_state.auth_error = "Missing PKCE/client info. Build Spotify login link again."
            return False

        token_url = "https://accounts.spotify.com/api/token"
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redir,
            "client_id": cid,
            "code_verifier": verifier,
        }
        r = requests.post(token_url, data=data, timeout=REQUEST_TIMEOUT)
        if r is None or r.status_code != 200:
            st.session_state.auth_error = f"Token exchange failed: {getattr(r,'status_code','—')} {getattr(r,'text','')}"
            return False

        tok = r.json()
        st.session_state.access_token = tok.get("access_token")
        st.session_state.refresh_token = tok.get("refresh_token")
        expires_in = tok.get("expires_in", 3600)
        st.session_state.expires_at = now_utc() + timedelta(seconds=expires_in - 30)
        st.session_state.authed = True

        # clear one-time vals
        st.session_state.pkce_code_verifier = None
        st.session_state.oauth_state_csrf = None
        st.session_state.auth_error = ""
        return True
    except Exception as e:
        st.session_state.auth_error = f"Exception during token exchange: {e}"
        return False

def refresh_access_token() -> bool:
    cid = st.session_state.spotify_client_id.strip()
    rt = st.session_state.refresh_token
    if not (cid and rt):
        return False
    token_url = "https://accounts.spotify.com/api/token"
    data = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": cid}
    r = requests.post(token_url, data=data, timeout=REQUEST_TIMEOUT)
    if r is None or r.status_code != 200:
        st.session_state.auth_error = f"Refresh failed: {getattr(r,'status_code','—')} {getattr(r,'text','')}"
        return False
    tok = r.json()
    st.session_state.access_token = tok.get("access_token")
    expires_in = tok.get("expires_in", 3600)
    st.session_state.expires_at = now_utc() + timedelta(seconds=expires_in - 30)
    st.session_state.authed = True
    return True

def ensure_token() -> bool:
    if not st.session_state.access_token:
        return False
    return True if not token_expired() else refresh_access_token()

def spotify_get(path: str, params=None):
    if not ensure_token():
        return None
    url = f"https://api.spotify.com/v1/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {st.session_state.access_token}"}
    r = requests.get(url, headers=headers, params=params or {}, timeout=REQUEST_TIMEOUT)
    if r.status_code == 401 and refresh_access_token():
        headers = {"Authorization": f"Bearer {st.session_state.access_token}"}
        r = requests.get(url, headers=headers, params=params or {}, timeout=REQUEST_TIMEOUT)
    if r.status_code >= 400:
        st.session_state.auth_error = f"Spotify API error {r.status_code}: {r.text}"
        return None
    return r.json()

# ---------- Cached enrichments ----------
@st.cache_data(ttl=120, show_spinner=False)
def _audio_features_csv(ids_csv: str):
    return spotify_get("audio-features", params={"ids": ids_csv}) or {}

def get_audio_features(track_ids):
    if not track_ids: return {}
    data = _audio_features_csv(",".join(track_ids[:100]))
    out = {}
    for f in (data.get("audio_features") or []):
        if f:
            out[f["id"]] = {
                "danceability": f.get("danceability"),
                "energy": f.get("energy"),
                "valence": f.get("valence"),
                "tempo": f.get("tempo"),
            }
    return out

@st.cache_data(ttl=300, show_spinner=False)
def _artists_csv(ids_csv: str):
    return spotify_get("artists", params={"ids": ids_csv}) or {}

def get_artist_genres(artist_ids):
    if not artist_ids: return []
    data = _artists_csv(",".join(artist_ids[:50]))
    genres = []
    for a in (data.get("artists") or []):
        genres.extend(a.get("genres", []))
    seen, out = set(), []
    for g in genres:
        if g not in seen:
            seen.add(g); out.append(g)
        if len(out) >= 20: break
    return out

# ---------- Sidebar ----------
with st.sidebar:
    st.markdown("## 🔐 Credentials (session-only)")
    st.session_state.spotify_client_id = st.text_input("Spotify Client ID", value=st.session_state.spotify_client_id)
    st.session_state.redirect_uri = st.text_input(
        "Redirect URI (must match Spotify app exactly)",
        value=st.session_state.redirect_uri or "https://<your-app>.streamlit.app",
    )
    st.session_state.openai_api_key = st.text_input("OpenAI API Key (optional, user-provided)", type="password")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("🔗 Build Spotify Login Link", use_container_width=True):
            url = build_auth_url()
            if url:
                st.session_state.last_auth_url = url
            else:
                st.error("Enter Client ID and Redirect URI first.")
        if st.session_state.get("last_auth_url"):
            st.link_button("Continue to Spotify →", st.session_state.last_auth_url, use_container_width=True)
            st.caption("If blocked, click the button again.")

    with c2:
        if st.button("🚪 Log out", use_container_width=True):
            for k in [
                "access_token","refresh_token","expires_at","authed",
                "pkce_code_verifier","oauth_state_csrf","auth_error",
                "recent_cache","mood_json","last_auth_url"
            ]:
                st.session_state[k] = None
            st.rerun()

    with st.expander("🔧 Debug"):
        dbg = {
            "authed": st.session_state.authed,
            "has_access_token": bool(st.session_state.access_token),
            "has_refresh_token": bool(st.session_state.refresh_token),
            "expires_at": str(st.session_state.expires_at),
            "auth_error": st.session_state.auth_error,
        }
        st.code(json.dumps(dbg, indent=2))

    with st.expander("🌐 Incoming query params"):
        try:
            qp_new = getattr(st, "query_params", None)
            st.write(dict(qp_new) if qp_new else st.experimental_get_query_params())
        except Exception as e:
            st.write(f"qp error: {e}")

# ---------- Handle OAuth redirect (ALWAYS restore from sticky state) ----------
error_val = read_qp_single("error")
error_desc = read_qp_single("error_description")
if error_val:
    st.error(f"Spotify returned error: {error_val} - {error_desc or ''}".strip())

code_val = read_qp_single("code")
state_val = read_qp_single("state")

restored = {"cid": None, "ru": None, "verifier_len": None}
if state_val and "." in state_val:
    csrf_part, payload_b64 = state_val.split(".", 1)
    try:
        payload = json.loads(b64url_decode_str(payload_b64).decode("utf-8"))
    except Exception:
        payload = {}
    # ALWAYS overwrite from state to ensure exact match
    if payload.get("v"):
        st.session_state.pkce_code_verifier = payload["v"]
        restored["verifier_len"] = len(payload["v"])
    if payload.get("cid"):
        st.session_state.spotify_client_id = payload["cid"]
        restored["cid"] = payload["cid"][:6] + "…"
    if payload.get("ru"):
        st.session_state.redirect_uri = payload["ru"]
        restored["ru"] = payload["ru"]
    st.session_state.oauth_state_csrf = csrf_part

with st.expander("🔎 OAuth Debug (restored)"):
    st.write(restored)

# Immediately exchange if we have a code and we’re not authed yet
if code_val and not st.session_state.authed:
    ok = exchange_code_for_token(code_val)
    if ok:
        st.success("Spotify authentication complete.")
        try:
            st.experimental_set_query_params()  # clear URL
        except Exception:
            pass
    else:
        st.error(st.session_state.auth_error or "Authentication failed.")

# ---------- Main UI ----------
st.title("🎧 Spotify Now Playing + Mood (PKCE, public-safe)")

if st.session_state.authed:
    st.caption("Authenticated. Auto-refreshing Now Playing every 10s.")
    st.autorefresh(interval=10_000, key="auto_refresh_key")
else:
    st.info("Paste Client ID + Redirect URI → Build Spotify Login Link → authorize. No client secret needed (PKCE).")
    if st.session_state.auth_error:
        st.error(st.session_state.auth_error)
    st.stop()

# ---- Now Playing ----
st.subheader("Now Playing")
now_playing = spotify_get("me/player/currently-playing", params={"additional_types": "track"})
if not now_playing or now_playing.get("item") is None:
    st.write("Nothing is currently playing.")
else:
    item = now_playing["item"]
    images = (item.get("album", {}).get("images") or [])
    img = images[IMG_INDEX]["url"] if images else None
    l, r = st.columns([1, 2])
    with l:
        if img:
            st.image(img, use_container_width=True)
        st.metric("Playing", "Yes" if now_playing.get("is_playing") else "No")
        st.metric("Progress", f"{ms_fmt(now_playing.get('progress_ms'))} / {ms_fmt(item.get('duration_ms'))}")
        st.metric("Popularity", item.get("popularity", "-"))
        st.metric("Explicit", "✔️" if item.get("explicit") else "—")
    with r:
        st.markdown(f"### {item.get('name')}")
        st.markdown("**Artist(s):** " + ", ".join([a["name"] for a in item.get("artists", [])]))
        st.markdown("**Album:** " + item.get("album", {}).get("name", "-"))
        tid = item.get("id")
        feats = get_audio_features([tid]).get(tid, {}) if tid else {}
        if feats:
            c = st.columns(4)
            c[0].metric("Danceability", f"{feats.get('danceability', 0):.2f}")
            c[1].metric("Energy", f"{feats.get('energy', 0):.2f}")
            c[2].metric("Valence", f"{feats.get('valence', 0):.2f}")
            c[3].metric("Tempo", f"{feats.get('tempo', 0):.0f} BPM")
            st.caption("Valence ≈ positivity; Danceability/Energy are Spotify audio features.")

# ---- Recent Tracks & Mood ----
st.subheader("Recent Tracks & Mood")
n = st.slider("How many recent tracks?", 1, 50, 20, 1)

def load_recent(limit: int):
    data = spotify_get("me/player/recently-played", params={"limit": limit})
    if not data or "items" not in data:
        return [], []
    rows, tids, aids = [], [], []
    for it in data["items"]:
        t = it.get("track", {})
        if not t: continue
        tids.append(t.get("id"))
        aids.extend([a.get("id") for a in t.get("artists", [])] if t.get("artists") else [])
        rows.append({
            "played_at": it.get("played_at"),
            "track_id": t.get("id"),
            "track": t.get("name"),
            "artists": ", ".join([a.get("name","") for a in t.get("artists", [])]),
            "album": t.get("album", {}).get("name"),
            "dur": ms_fmt(t.get("duration_ms")),
            "pop": t.get("popularity"),
        })
    feats = get_audio_features([x for x in tids if x])
    genres = get_artist_genres(list(dict.fromkeys(aids)))
    for r in rows:
        ft = feats.get(r["track_id"], {})
        r["dance"] = round((ft.get("danceability") or 0), 2)
        r["energy"] = round((ft.get("energy") or 0), 2)
        r["valence"] = round((ft.get("valence") or 0), 2)
        r["bpm"] = round((ft.get("tempo") or 0))
    return rows, genres

if (st.session_state.recent_cache is None or
    st.session_state.recent_cache.get("n") != n):
    rows, genres = load_recent(n)
    st.session_state.recent_cache = {"n": n, "rows": rows, "genres": genres}
else:
    rows = st.session_state.recent_cache["rows"]
    genres = st.session_state.recent_cache["genres"]

if not rows:
    st.warning("No recent tracks found.")
else:
    st.dataframe(
        [
            {"Played": r["played_at"], "Track": r["track"], "Artists": r["artists"],
             "Album": r["album"], "Dur": r["dur"], "Pop": r["pop"],
             "Dance": r["dance"], "Energy": r["energy"], "Valence": r["valence"], "BPM": r["bpm"]}
            for r in rows
        ],
        use_container_width=True, hide_index=True,
    )

    st.markdown("### 🧠 AI Mood (local only; key stays in your session)")
    extra = st.text_area("Optional context (e.g., 'late-night focus', 'post-gym')", "")
    model = st.selectbox("OpenAI model", ["gpt-4.1-mini","gpt-4o-mini","gpt-4.1","gpt-3.5-turbo"], index=0)

    if st.button("Analyze Mood", type="primary"):
        if not st.session_state.openai_api_key:
            st.error("Paste your OpenAI API key in the sidebar.")
        else:
            try:
                from openai import OpenAI  # lazy import
                client = OpenAI(api_key=st.session_state.openai_api_key)
            except Exception as e:
                st.error(f"OpenAI import/init failed: {e}")
                st.stop()

            compact = [
                {"track": r["track"], "artists": r["artists"], "pop": r["pop"],
                 "dance": r["dance"], "energy": r["energy"], "valence": r["valence"], "bpm": r["bpm"]}
                for r in rows
            ]
            sys_msg = ("You are an expert music psychologist. Infer mood and energy from Spotify audio features "
                       "(valence/energy/danceability/tempo) and genres. Be precise and practical.")
            schema = ("Return strict JSON: {mood:str, energy_level:'low'|'medium'|'high', confidence:0..1, "
                      "tags:[3..7 strings], one_sentence_summary:str, suggested_action:str, suggested_playlist_title:str}")

            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": sys_msg},
                        {"role": "user", "content": schema},
                        {"role": "user", "content": json.dumps({"genres": genres, "tracks": compact, "notes": extra})},
                    ],
                    temperature=0.5,
                    max_tokens=350,
                )
                raw = resp.choices[0].message.content.strip()
                if raw.startswith("```"):
                    raw = raw.strip("`").split("\n", 1)[-1]
                st.session_state.mood_json = json.loads(raw)
            except Exception as e:
                st.error(f"OpenAI error: {e}")

    if st.session_state.mood_json:
        mj = st.session_state.mood_json
        st.success(mj.get("one_sentence_summary", ""))
        c1, c2, c3 = st.columns(3)
        c1.metric("Mood", mj.get("mood", "-"))
        c2.metric("Energy", mj.get("energy_level", "-"))
        conf = mj.get("confidence")
        c3.metric("Confidence", f"{conf:.0%}" if isinstance(conf, (int, float)) else "-")
        st.write("**Tags:** " + ", ".join(mj.get("tags", [])))
        st.write("**Action:** " + mj.get("suggested_action", ""))
        st.write("_Playlist idea:_ **" + mj.get("suggested_playlist_title", "") + "**")

st.markdown("---")
st.caption("PKCE with sticky OAuth state (always restored). Public-safe; session-only storage.")
