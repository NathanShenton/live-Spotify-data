import os, json, base64, hashlib, secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
import requests
import streamlit as st

# --------- OpenAI SDK (optional, user-supplied key) ----------
try:
    from openai import OpenAI
    openai_available = True
except Exception:
    openai_available = False

st.set_page_config(page_title="Spotify Now + Mood (PKCE)", page_icon="🎧", layout="wide")

SCOPES = [
    "user-read-playback-state",
    "user-read-currently-playing",
    "user-read-recently-played",
]

def init_state():
    ss = st.session_state
    ss.setdefault("spotify_client_id", "")
    ss.setdefault("redirect_uri", "")
    ss.setdefault("openai_api_key", "")
    ss.setdefault("pkce_code_verifier", None)
    ss.setdefault("oauth_state", None)
    ss.setdefault("access_token", None)
    ss.setdefault("refresh_token", None)
    ss.setdefault("expires_at", None)
    ss.setdefault("authed", False)
    ss.setdefault("last_auth_url", None)
    ss.setdefault("mood_json", None)
    ss.setdefault("recent_cache", None)

init_state()

def now_utc():
    return datetime.now(timezone.utc)

def token_expired():
    exp = st.session_state.get("expires_at")
    return (not exp) or (now_utc() >= exp)

# ---------------- PKCE helpers ----------------
def gen_code_verifier():
    # 43..128 chars, allowed [A-Z a-z 0-9 -._~]
    return base64.urlsafe_b64encode(os.urandom(64)).decode().rstrip("=")

def code_challenge_s256(verifier: str):
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")

def new_state_token():
    return secrets.token_urlsafe(16)

# ---------------- Spotify OAuth (PKCE) ----------------
def build_auth_url():
    cid = st.session_state.spotify_client_id.strip()
    redir = st.session_state.redirect_uri.strip()
    if not cid or not redir:
        return None

    verifier = gen_code_verifier()
    challenge = code_challenge_s256(verifier)
    state_tok = new_state_token()

    st.session_state.pkce_code_verifier = verifier
    st.session_state.oauth_state = state_tok

    q = {
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": redir,
        "scope": " ".join(SCOPES),
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state_tok,
        "show_dialog": "false",
    }
    return f"https://accounts.spotify.com/authorize?{urlencode(q)}"

def exchange_code_for_token(code: str) -> bool:
    cid = st.session_state.spotify_client_id.strip()
    redir = st.session_state.redirect_uri.strip()
    verifier = st.session_state.pkce_code_verifier
    if not (cid and redir and verifier):
        st.error("Missing PKCE state; click ‘Get Login Link’ again.")
        return False

    token_url = "https://accounts.spotify.com/api/token"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redir,
        "client_id": cid,
        "code_verifier": verifier,
    }
    r = requests.post(token_url, data=data, timeout=30)
    if r.status_code != 200:
        st.error(f"Token exchange failed: {r.status_code} {r.text}")
        return False

    tok = r.json()
    st.session_state.access_token = tok.get("access_token")
    st.session_state.refresh_token = tok.get("refresh_token")
    expires_in = tok.get("expires_in", 3600)
    st.session_state.expires_at = now_utc() + timedelta(seconds=expires_in - 30)
    st.session_state.authed = True

    # one-time use
    st.session_state.pkce_code_verifier = None
    st.session_state.oauth_state = None
    return True

def refresh_access_token() -> bool:
    cid = st.session_state.spotify_client_id.strip()
    rt = st.session_state.refresh_token
    if not (cid and rt):
        return False
    token_url = "https://accounts.spotify.com/api/token"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": cid,
    }
    r = requests.post(token_url, data=data, timeout=30)
    if r.status_code != 200:
        st.error(f"Refresh failed: {r.status_code} {r.text}")
        return False
    tok = r.json()
    st.session_state.access_token = tok.get("access_token")
    expires_in = tok.get("expires_in", 3600)
    st.session_state.expires_at = now_utc() + timedelta(seconds=expires_in - 30)
    st.session_state.authed = True
    return True

def ensure_token():
    if not st.session_state.access_token:
        return False
    if token_expired():
        return refresh_access_token()
    return True

def spotify_get(path: str, params=None):
    if not ensure_token():
        return None
    url = f"https://api.spotify.com/v1/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {st.session_state.access_token}"}
    r = requests.get(url, headers=headers, params=params or {}, timeout=30)
    if r.status_code == 401 and refresh_access_token():
        headers = {"Authorization": f"Bearer {st.session_state.access_token}"}
        r = requests.get(url, headers=headers, params=params or {}, timeout=30)
    if r.status_code >= 400:
        return None
    return r.json()

def ms_fmt(ms):
    if ms is None: return "-"
    s = ms // 1000
    return f"{s//60}:{s%60:02d}"

def get_audio_features(track_ids):
    if not track_ids: return {}
    data = spotify_get("audio-features", params={"ids": ",".join(track_ids[:100])})
    out = {}
    if data and "audio_features" in data:
        for f in data["audio_features"] or []:
            if f:
                out[f["id"]] = {
                    "danceability": f.get("danceability"),
                    "energy": f.get("energy"),
                    "valence": f.get("valence"),
                    "tempo": f.get("tempo"),
                }
    return out

def get_artist_genres(artist_ids):
    if not artist_ids: return []
    data = spotify_get("artists", params={"ids": ",".join(artist_ids[:50])})
    genres = []
    if data and "artists" in data:
        for a in data["artists"]:
            genres.extend(a.get("genres", []))
    # dedupe
    seen, out = set(), []
    for g in genres:
        if g not in seen:
            seen.add(g)
            out.append(g)
        if len(out) >= 20:
            break
    return out

# ---------------- Sidebar: creds input (no secrets in repo) ----------------
with st.sidebar:
    st.markdown("## 🔐 Credentials (runtime only)")
    st.session_state.spotify_client_id = st.text_input("Spotify Client ID", value=st.session_state.spotify_client_id)
    st.session_state.redirect_uri = st.text_input(
        "Redirect URI (must match Spotify app)",
        value=st.session_state.redirect_uri or "https://<your-streamlit-app>.streamlit.app"
    )
    st.session_state.openai_api_key = st.text_input("OpenAI API Key (optional; user-owned)", type="password")

    colA, colB = st.columns(2)
    with colA:
        if st.button("🔗 Get Spotify Login Link", use_container_width=True):
            url = build_auth_url()
            if url:
                st.session_state.last_auth_url = url
            else:
                st.error("Enter Client ID and Redirect URI first.")
    with colB:
        if st.button("🚪 Log out", use_container_width=True):
            for k in ["access_token","refresh_token","expires_at","authed","pkce_code_verifier","oauth_state"]:
                st.session_state[k] = None
            st.rerun()
    if st.session_state.last_auth_url:
        st.markdown(f"[Click to authorize Spotify]({st.session_state.last_auth_url})")
    st.caption("Nothing is saved server-side. Values live only in your session.")

# -------------- Handle redirect ?code= & ?state= ----------------
def get_query_params():
    try:
        return st.query_params  # Streamlit >=1.32
    except Exception:
        return st.experimental_get_query_params()

qp = get_query_params()
if "code" in qp:
    code_val = qp["code"][0] if isinstance(qp["code"], list) else qp["code"]
    state_val = qp.get("state", [None])[0] if isinstance(qp.get("state"), list) else qp.get("state")
    if st.session_state.oauth_state and state_val != st.session_state.oauth_state:
        st.error("State mismatch. Start the login again.")
    elif exchange_code_for_token(code_val):
        st.success("Spotify authentication complete.")
        # clear URL params
        try:
            st.experimental_set_query_params()
        except Exception:
            pass

# auto-refresh every 10s when authed
if st.session_state.authed:
    st.autorefresh(interval=10_000, key="auto_refresh")

# ---------------- Main UI ----------------
st.title("🎧 Spotify Now Playing + Mood (Public-safe PKCE)")

if not st.session_state.authed:
    st.info("Authorize with Spotify from the sidebar. This app uses PKCE—no client secret required.")
    st.stop()

# ---- Now Playing ----
st.subheader("Now Playing (10s auto-refresh)")
now = spotify_get("me/player/currently-playing", params={"additional_types": "track"})
if not now or now.get("item") is None:
    st.write("Nothing is currently playing.")
else:
    item = now["item"]
    img = (item.get("album", {}).get("images") or [{}])[0].get("url")
    left, right = st.columns([1,2])
    with left:
        if img: st.image(img, use_container_width=True)
        st.metric("Playing", "Yes" if now.get("is_playing") else "No")
        st.metric("Progress", f"{ms_fmt(now.get('progress_ms'))} / {ms_fmt(item.get('duration_ms'))}")
        st.metric("Popularity", item.get("popularity", "-"))
        st.metric("Explicit", "✔️" if item.get("explicit") else "—")
    with right:
        st.markdown(f"### {item.get('name')}")
        st.markdown("**Artist(s):** " + ", ".join([a["name"] for a in item.get("artists", [])]))
        st.markdown("**Album:** " + item.get("album", {}).get("name", "-"))
        tid = item.get("id")
        feats = get_audio_features([tid]).get(tid, {}) if tid else {}
        if feats:
            c = st.columns(4)
            c[0].metric("Danceability", f"{feats.get('danceability',0):.2f}")
            c[1].metric("Energy", f"{feats.get('energy',0):.2f}")
            c[2].metric("Valence", f"{feats.get('valence',0):.2f}")
            c[3].metric("Tempo", f"{feats.get('tempo',0):.0f} BPM")
            st.caption("Valence ≈ positivity; Danceability/Energy are Spotify audio features.")

# ---- Recent Tracks + Mood ----
st.subheader("Recent Tracks & Mood")
n = st.slider("How many recent tracks?", 1, 50, 20, 1)

def load_recent(n):
    data = spotify_get("me/player/recently-played", params={"limit": n})
    if not data or "items" not in data: return [], []
    rows, tids, aids = [], [], []
    for it in data["items"]:
        t = it.get("track", {})
        if not t: continue
        tids.append(t.get("id"))
        aids.extend([a.get("id") for a in t.get("artists", []) if a.get("id")])
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
        r.update({
            "dance": round(ft.get("danceability",0) or 0, 2),
            "energy": round(ft.get("energy",0) or 0, 2),
            "valence": round(ft.get("valence",0) or 0, 2),
            "bpm": round(ft.get("tempo",0) or 0),
        })
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
            {
                "Played": r["played_at"],
                "Track": r["track"],
                "Artists": r["artists"],
                "Album": r["album"],
                "Dur": r["dur"],
                "Pop": r["pop"],
                "Dance": r["dance"],
                "Energy": r["energy"],
                "Valence": r["valence"],
                "BPM": r["bpm"],
            } for r in rows
        ],
        use_container_width=True,
        hide_index=True
    )

    st.markdown("### 🧠 AI Mood (your key only; nothing stored)")
    extra = st.text_area("Optional context (e.g., 'late-night focus', 'post-gym')", "")
    model = st.selectbox("OpenAI model", ["gpt-4.1-mini","gpt-4o-mini","gpt-4.1","gpt-3.5-turbo"], index=0)

    if st.button("Analyze Mood", type="primary"):
        if not openai_available:
            st.error("OpenAI SDK not installed on server. Add `openai>=1.14.0` to requirements.")
        elif not st.session_state.openai_api_key:
            st.error("Please paste your OpenAI key in the sidebar.")
        else:
            client = OpenAI(api_key=st.session_state.openai_api_key)
            compact = [
                {
                    "track": r["track"], "artists": r["artists"], "pop": r["pop"],
                    "dance": r["dance"], "energy": r["energy"], "valence": r["valence"], "bpm": r["bpm"]
                } for r in rows
            ]
            sys = ("You are an expert music psychologist. Infer mood/energy from Spotify audio features "
                   "(valence/energy/danceability/tempo) and genres. Be precise and practical.")
            schema = ("Return strict JSON: {mood:str, energy_level:'low'|'medium'|'high', confidence:0..1, "
                      "tags:[3..7 strings], one_sentence_summary:str, suggested_action:str, suggested_playlist_title:str}")
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role":"system","content":sys},
                        {"role":"user","content":schema},
                        {"role":"user","content":json.dumps({"genres":genres, "tracks":compact, "notes":extra})},
                    ],
                    temperature=0.5, max_tokens=350
                )
                raw = resp.choices[0].message.content.strip()
                if raw.startswith("```"):
                    raw = raw.strip("`").split("\n",1)[-1]
                st.session_state.mood_json = json.loads(raw)
            except Exception as e:
                st.error(f"OpenAI error: {e}")

    if st.session_state.mood_json:
        mj = st.session_state.mood_json
        st.success(mj.get("one_sentence_summary",""))
        c1,c2,c3 = st.columns(3)
        c1.metric("Mood", mj.get("mood","-"))
        c2.metric("Energy", mj.get("energy_level","-"))
        conf = mj.get("confidence")
        c3.metric("Confidence", f"{conf:.0%}" if isinstance(conf,(int,float)) else "-")
        st.write("**Tags:** " + ", ".join(mj.get("tags", [])))
        st.write("**Action:** " + mj.get("suggested_action",""))
        st.write("_Playlist idea:_ **" + mj.get("suggested_playlist_title","") + "**")

st.markdown("---")
st.caption("Public-safe: Spotify PKCE (no client secret), user-supplied OpenAI key, session-only storage.")
