# Streamlit + Spotify Now Playing + Mood (Auth Code via Spotipy)
# - Only st.query_params (no experimental_* APIs)
# - Secrets from Streamlit Cloud (no repo secrets)
# - Tight timeouts, smallest images, caching; lazy OpenAI import
# - Same-tab authorize; immediate exchange; robust refresh via JS

from typing import Optional, Dict, Any, List
import json
from datetime import datetime, timezone

import streamlit as st
import spotipy
from spotipy.oauth2 import SpotifyOAuth

# -------------------- App constants --------------------
REQUEST_TIMEOUT = 8           # seconds
IMG_INDEX = -1                # use smallest album image
SCOPES = [
    "user-read-playback-state",
    "user-read-currently-playing",
    "user-read-recently-played",
]

st.set_page_config(page_title="Spotify Now + Mood", page_icon="🎧", layout="wide")

# -------------------- Auto-refresh (JS) --------------------
def enable_auto_refresh(seconds: int = 10):
    st.markdown(
        f"""
        <script>
        setTimeout(function() {{
            const url = new URL(window.location);
            url.searchParams.set('_ts', Date.now().toString()); // bust cache, trigger rerun
            window.location.replace(url);
        }}, {int(seconds*1000)});
        </script>
        """,
        unsafe_allow_html=True,
    )

# -------------------- Secrets (Streamlit Cloud) --------------------
def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    try:
        return st.secrets[name]  # type: ignore[index]
    except Exception:
        return default

CLIENT_ID = get_secret("SPOTIFY_CLIENT_ID", "")
CLIENT_SECRET = get_secret("SPOTIFY_CLIENT_SECRET", "")
REDIRECT_URI = get_secret("SPOTIFY_REDIRECT_URI", "")  # must match Spotify app exactly

# -------------------- Session init --------------------
def _init_state():
    ss = st.session_state
    ss.setdefault("token_info", None)   # spotipy token dict
    ss.setdefault("authed", False)
    ss.setdefault("auth_error", "")
    ss.setdefault("openai_api_key", "")
    ss.setdefault("mood_json", None)
    ss.setdefault("auto_refresh_on", True)
_init_state()

# -------------------- Helpers --------------------
def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()

def ms_fmt(ms: Optional[int]) -> str:
    if ms is None:
        return "-"
    s = ms // 1000
    return f"{s//60}:{s%60:02d}"

def masked(s: Optional[str]) -> str:
    if not s: return "—"
    return s[:6] + "…" if len(s) > 6 else s

def qp_get(name: str) -> Optional[str]:
    """Read a single query param using NEW API only."""
    try:
        v = st.query_params.get(name)
        return v[0] if isinstance(v, list) else v
    except Exception:
        return None

def qp_clear(*keys: str) -> None:
    try:
        for k in keys:
            if k in st.query_params:
                del st.query_params[k]
    except Exception:
        pass

# -------------------- OAuth via Spotipy --------------------
def make_oauth() -> SpotifyOAuth:
    return SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=" ".join(SCOPES),
        cache_path=None,
        show_dialog=False,
        open_browser=False,
        requests_timeout=REQUEST_TIMEOUT,
    )

def ensure_token() -> Optional[str]:
    """Return a valid access_token or None."""
    ti = st.session_state.token_info
    if not ti:
        return None
    expires_at = float(ti.get("expires_at", 0))
    if now_ts() >= (expires_at - 30):
        try:
            sp_oauth = make_oauth()
            new_ti = sp_oauth.refresh_access_token(ti["refresh_token"])
            st.session_state.token_info = new_ti
            return new_ti.get("access_token")
        except Exception as e:
            st.session_state.auth_error = f"Refresh failed: {e}"
            return None
    return ti.get("access_token")

def spotify_client() -> Optional[spotipy.Spotify]:
    token = ensure_token()
    if not token:
        return None
    return spotipy.Spotify(auth=token, requests_timeout=REQUEST_TIMEOUT)

# -------------------- Sidebar: Secrets + Login --------------------
with st.sidebar:
    st.markdown("## 🔐 Secrets")
    ok = True
    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        st.error("Add SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI in Streamlit **Secrets**.")
        st.caption("Streamlit Cloud → Your app → Settings → Secrets.")
        ok = False

    st.markdown("## 🤖 OpenAI (optional)")
    st.session_state.openai_api_key = st.text_input(
        "OpenAI API Key", type="password", value=st.session_state.openai_api_key
    )

    st.markdown("## ⚙️ Auto-refresh")
    st.session_state.auto_refresh_on = st.toggle("Auto-refresh every 10s", value=st.session_state.auto_refresh_on)

    st.markdown("## 🎫 Login")
    if ok:
        try:
            sp_oauth = make_oauth()
            auth_url = sp_oauth.get_authorize_url()
            st.link_button("Continue to Spotify →", auth_url, use_container_width=True)
        except Exception as e:
            st.error(f"OAuth init error: {e}")

        # --- Handle redirect back using NEW query params API ---
        err = qp_get("error")
        if err:
            st.error(f"Spotify error: {err}")

        code = qp_get("code")
        if code and not st.session_state.authed:
            try:
                token_info = sp_oauth.get_access_token(code, as_dict=True)
                st.session_state.token_info = token_info
                st.session_state.authed = True
                st.success("Authenticated with Spotify.")
                # Clear params from URL using NEW API
                qp_clear("code", "state", "_ts")
            except Exception as e:
                st.session_state.auth_error = f"Token exchange failed: {e}"
                st.error(st.session_state.auth_error)

    st.markdown("## 🔧 Debug")
    dbg = {
        "authed": st.session_state.authed,
        "has_token_info": bool(st.session_state.token_info),
        "client_id": masked(CLIENT_ID),
        "redirect_uri": REDIRECT_URI or "—",
        "auth_error": st.session_state.auth_error or "—",
    }
    st.code(json.dumps(dbg, indent=2))

    if st.button("🚪 Log out", use_container_width=True):
        for k in ["token_info", "authed", "auth_error", "mood_json"]:
            st.session_state[k] = None
        st.rerun()

# -------------------- Early exit if not authed --------------------
if not st.session_state.authed:
    st.title("🎧 Spotify Now Playing + Mood")
    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        st.error("Add your Spotify secrets in Streamlit **Secrets** to proceed.")
    else:
        st.info("Click **Continue to Spotify →**, approve, and you’ll be redirected back.")
    st.stop()

# -------------------- Main UI (after auth) --------------------
st.title("🎧 Spotify Now Playing + Mood")
if st.session_state.auto_refresh_on:
    st.caption("Auto-refreshing Now Playing every 10s.")
    enable_auto_refresh(10)
else:
    st.caption("Auto-refresh is OFF.")

sp = spotify_client()
if not sp:
    st.error(st.session_state.auth_error or "Missing/expired token.")
    st.stop()

# ---- Now Playing ----
st.subheader("Now Playing")
try:
    np = sp.current_user_playing_track()
except spotipy.SpotifyException as e:
    st.error(f"Spotify error: {e}")
    np = None

if not np or np.get("item") is None:
    st.write("Nothing is currently playing.")
else:
    item = np["item"]
    images = (item.get("album", {}).get("images") or [])
    img = images[IMG_INDEX]["url"] if images else None
    l, r = st.columns([1, 2])
    with l:
        if img:
            st.image(img, use_container_width=True)
        st.metric("Playing", "Yes" if np.get("is_playing") else "No")
        st.metric("Progress", f"{ms_fmt(np.get('progress_ms'))} / {ms_fmt(item.get('duration_ms'))}")
        st.metric("Popularity", item.get("popularity", "-"))
        st.metric("Explicit", "✔️" if item.get("explicit") else "—")
    with r:
        st.markdown(f"### {item.get('name')}")
        st.markdown("**Artist(s):** " + ", ".join([a["name"] for a in item.get("artists", [])]))
        st.markdown("**Album:** " + item.get("album", {}).get("name", "-"))

        # Audio features (cached)
        @st.cache_data(ttl=120, show_spinner=False)
        def _audio_features(token: str, track_ids: List[str]) -> Dict[str, Dict[str, Any]]:
            cli = spotipy.Spotify(auth=token, requests_timeout=REQUEST_TIMEOUT)
            ids = [x for x in track_ids if x][:100]
            feats = cli.audio_features(ids) or []
            out: Dict[str, Dict[str, Any]] = {}
            for f in feats:
                if not f: continue
                out[f["id"]] = {
                    "danceability": f.get("danceability"),
                    "energy": f.get("energy"),
                    "valence": f.get("valence"),
                    "tempo": f.get("tempo"),
                }
            return out

        tok = ensure_token()
        tid = item.get("id")
        feats = _audio_features(tok, [tid]).get(tid, {}) if (tok and tid) else {}
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

rows: List[Dict[str, Any]] = []
genres: List[str] = []

try:
    rec = sp.current_user_recently_played(limit=n) or {}
    items = rec.get("items", [])
    track_ids, artist_ids = [], []
    for it in items:
        t = it.get("track") or {}
        if not t: continue
        track_ids.append(t.get("id"))
        artist_ids.extend([a.get("id") for a in t.get("artists", []) if a.get("id")])
        rows.append({
            "played_at": it.get("played_at"),
            "track_id": t.get("id"),
            "track": t.get("name"),
            "artists": ", ".join([a.get("name", "") for a in t.get("artists", [])]),
            "album": t.get("album", {}).get("name"),
            "dur": ms_fmt(t.get("duration_ms")),
            "pop": t.get("popularity"),
        })

    @st.cache_data(ttl=120, show_spinner=False)
    def _features_block(token: str, ids: List[str]) -> Dict[str, Dict[str, Any]]:
        cli = spotipy.Spotify(auth=token, requests_timeout=REQUEST_TIMEOUT)
        feats = cli.audio_features([x for x in ids if x][:100]) or []
        out = {}
        for f in feats:
            if not f: continue
            out[f["id"]] = {
                "danceability": f.get("danceability"),
                "energy": f.get("energy"),
                "valence": f.get("valence"),
                "tempo": f.get("tempo"),
            }
        return out

    @st.cache_data(ttl=300, show_spinner=False)
    def _genres_block(token: str, artist_ids: List[str]) -> List[str]:
        cli = spotipy.Spotify(auth=token, requests_timeout=REQUEST_TIMEOUT)
        ids = list(dict.fromkeys([x for x in artist_ids if x]))[:50]
        arts = cli.artists(ids).get("artists", [])
        acc, seen = [], set()
        for a in arts:
            for g in a.get("genres", []):
                if g not in seen:
                    seen.add(g); acc.append(g)
                if len(acc) >= 20: return acc
        return acc

    tok = ensure_token()
    feats_map = _features_block(tok, track_ids) if (tok and track_ids) else {}
    genres = _genres_block(tok, artist_ids) if (tok and artist_ids) else []

    for r in rows:
        ft = feats_map.get(r["track_id"], {})
        r["Dance"] = round((ft.get("danceability") or 0), 2)
        r["Energy"] = round((ft.get("energy") or 0), 2)
        r["Valence"] = round((ft.get("valence") or 0), 2)
        r["BPM"] = round((ft.get("tempo") or 0))

except spotipy.SpotifyException as e:
    st.error(f"Spotify error: {e}")

if not rows:
    st.warning("No recent tracks found.")
else:
    st.dataframe(
        [
            {
                "Played": r["played_at"], "Track": r["track"], "Artists": r["artists"],
                "Album": r["album"], "Dur": r["dur"], "Pop": r["pop"],
                "Dance": r["Dance"], "Energy": r["Energy"], "Valence": r["Valence"], "BPM": r["BPM"],
            } for r in rows
        ],
        use_container_width=True, hide_index=True,
    )

    st.markdown("### 🧠 AI Mood (local only; key stays in your session)")
    extra = st.text_area("Optional context (e.g., 'late-night focus', 'post-gym')", "")
    model = st.selectbox("OpenAI model", ["gpt-4.1-mini","gpt-4o-mini","gpt-4.1","gpt-3.5-turbo"], index=0)

    if st.button("Analyze Mood", type="primary"):
        if not st.session_state.openai_api_key:
            st.error("Paste your OpenAI API key above.")
        else:
            try:
                from openai import OpenAI  # lazy import
                client = OpenAI(api_key=st.session_state.openai_api_key)
            except Exception as e:
                st.error(f"OpenAI import/init failed: {e}")
                st.stop()

            compact = [
                {"track": r["track"], "artists": r["artists"], "pop": r["pop"],
                 "dance": r["Dance"], "energy": r["Energy"], "valence": r["Valence"], "bpm": r["BPM"]}
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
st.caption("Authorization Code flow via Spotipy. Only st.query_params used; tokens auto-refreshed. JS-based auto-refresh for 1.36.")