import os
from datetime import datetime, timezone
from typing import Dict, List

import streamlit as st
from supabase import create_client, Client

# Optional autorefresh component; fallback to sleep if not installed
try:
    from streamlit_autorefresh import st_autorefresh
    HAS_REFRESH = True
except Exception:
    HAS_REFRESH = False
import time

# Load local .env if available (harmless on Streamlit Cloud)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ---- Config ----
def get_secret(k: str):
    return os.getenv(k) or st.secrets.get(k)

SUPABASE_URL = get_secret("SUPABASE_URL")
SUPABASE_ANON_KEY = get_secret("SUPABASE_ANON_KEY")

st.set_page_config(page_title="Timed Quiz", page_icon="⏱️", layout="wide")

if not SUPABASE_URL or not SUPABASE_ANON_KEY:
    st.error("Missing Supabase config. Add SUPABASE_URL and SUPABASE_ANON_KEY to .env (local) or Secrets (Cloud).")
    st.stop()

def sb_client() -> Client:
    client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    if "session" in st.session_state:
        s = st.session_state["session"]
        client.auth.set_session(s["access_token"], s["refresh_token"])
    return client

# ---- Utilities ----
def seconds_left(ends_at_val) -> int:
    """Compute remaining seconds to a server-provided ISO timestamp."""
    try:
        if isinstance(ends_at_val, str):
            s = ends_at_val.strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)  # supports ' ' or 'T'
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        elif isinstance(ends_at_val, datetime):
            dt = ends_at_val if ends_at_val.tzinfo else ends_at_val.replace(tzinfo=timezone.utc)
        else:
            return 0
        remaining = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0, int(remaining))
    except Exception:
        return 0

# ---- Supabase RPC wrappers ----
def rpc_start_attempt(client: Client, quiz_id: str):
    res = client.rpc("start_attempt", {"p_quiz_id": quiz_id}).execute()
    row = res.data[0]
    return row["attempt_id"], row["started_at"], row["ends_at"]

def rpc_finish_attempt(client: Client, attempt_id: str, answers: List[Dict]):
    res = client.rpc("finish_attempt", {"p_attempt_id": attempt_id, "p_answers": answers}).execute()
    return res.data

def rpc_get_results(client: Client, attempt_id: str):
    res = client.rpc("get_results", {"p_attempt_id": attempt_id}).execute()
    return res.data

# ---- Auth UI ----
def auth_ui(client: Client):
    st.sidebar.header("Account")
    if st.session_state.get("user"):
        user = st.session_state["user"]
        st.sidebar.success(f"Signed in as {user['email']}")
        if st.sidebar.button("Sign out"):
            client.auth.sign_out()
            for k in ["user", "session", "attempt", "answers", "results", "choice_text_by_id"]:
                st.session_state.pop(k, None)
            st.rerun()
        return

    tab_login, tab_signup = st.sidebar.tabs(["Login", "Sign up"])
    with tab_login:
        email = st.text_input("Email", key="login_email")
        password = st.text_input("Password", type="password", key="login_password")
        if st.button("Login"):
            try:
                res = client.auth.sign_in_with_password({"email": email, "password": password})
                st.session_state["session"] = {
                    "access_token": res.session.access_token,
                    "refresh_token": res.session.refresh_token,
                }
                st.session_state["user"] = {"id": res.user.id, "email": res.user.email}
                st.rerun()
            except Exception as e:
                st.error(f"Login failed: {e}")

    with tab_signup:
        email2 = st.text_input("Email ", key="signup_email")
        pw1 = st.text_input("Password ", type="password", key="signup_pw1")
        pw2 = st.text_input("Confirm Password", type="password", key="signup_pw2")
        if st.button("Create account"):
            if not email2 or not pw1 or pw1 != pw2:
                st.error("Please fill all fields and ensure passwords match.")
            else:
                try:
                    client.auth.sign_up({"email": email2, "password": pw1})
                    st.success("Account created. Please login.")
                except Exception as e:
                    st.error(f"Sign up failed: {e}")

# ---- Data fetch ----
def list_quizzes(client: Client):
    res = client.table("quizzes").select("id,title,description,time_limit_seconds").eq("is_published", True).order("created_at").execute()
    return res.data or []

def fetch_quiz_bundle(client: Client, quiz_id: str):
    """Return (questions, choices_by_question, choice_text_by_id) or ([], {}, {}) on issue."""
    try:
        qres = client.table("questions").select("id,body,explanation,display_order").eq("quiz_id", quiz_id).order("display_order").execute()
    except Exception as e:
        st.error(f"Failed to load questions: {e}")
        return [], {}, {}
    qs = qres.data or []
    if not qs:
        return [], {}, {}

    qids = [q["id"] for q in qs]
    try:
        cres = client.table("choices").select("id,question_id,body").in_("question_id", qids).execute()
        ch = cres.data or []
    except Exception as e:
        st.error(f"Failed to load choices: {e}")
        return qs, {}, {}

    choices_by_q = {}
    choice_text_by_id = {}
    for c in ch:
        choices_by_q.setdefault(c["question_id"], []).append(c)
        choice_text_by_id[c["id"]] = c["body"]

    st.session_state["choice_text_by_id"] = choice_text_by_id
    return qs, choices_by_q, choice_text_by_id

# ---- Attempt hydration (handles restarts) ----
def hydrate_attempt_status(client: Client):
    att = st.session_state.get("attempt")
    if not att:
        return
    try:
        res = client.table("attempts").select("id,status,started_at,quiz_id").eq("id", att["id"]).single().execute()
        row = res.data
        if not row:
            return
        if row["status"] == "finished":
            att["submitted"] = True
            st.session_state["attempt"] = att
    except Exception:
        pass

# ---- Submit ----
def do_submit(client: Client):
    attempt = st.session_state["attempt"]
    if attempt["submitted"]:
        return
    answers = [{"question_id": qid, "chosen_choice_id": cid} for qid, cid in (st.session_state.get("answers") or {}).items()]
    try:
        data = rpc_finish_attempt(client, attempt["id"], answers)
        st.session_state["results"] = data
        attempt["submitted"] = True
        st.session_state["attempt"] = attempt
        st.rerun()
    except Exception as e:
        msg = str(e)
        if "already finished" in msg:
            attempt["submitted"] = True
            st.session_state["attempt"] = attempt
            st.info("Attempt was already finished.")
            st.rerun()
        else:
            st.error(f"Submit failed: {e}")

# ---- Main quiz UI ----
def render_quiz(client: Client):
    st.title("⏱️ Timed Quiz")

    if not st.session_state.get("user"):
        st.info("Please login on the left to take a quiz.")
        return

    hydrate_attempt_status(client)

    if "attempt" not in st.session_state:
        quizzes = list_quizzes(client)
        if not quizzes:
            st.warning("No published quizzes.")
            return
        q_options = {f"{q['title']} ({q['time_limit_seconds']}s)": q["id"] for q in quizzes}
        label = st.selectbox("Choose a quiz", list(q_options.keys()))
        if st.button("Start quiz"):
            for k in ["attempt", "answers", "results"]:
                st.session_state.pop(k, None)
            quiz_id = q_options[label]
            try:
                attempt_id, started, ends = rpc_start_attempt(client, quiz_id)
                st.session_state["attempt"] = {
                    "id": attempt_id,
                    "quiz_id": quiz_id,
                    "started_at": started,
                    "ends_at": ends,
                    "submitted": False
                }
                st.session_state["answers"] = {}
                st.rerun()
            except Exception as e:
                st.error(f"Could not start attempt: {e}")
        return

    attempt = st.session_state["attempt"]
    left, right = st.columns([2,1])

    with right:
        remaining = seconds_left(attempt["ends_at"])
        m, s = divmod(remaining, 60)
        st.metric("Time left", f"{m:02d}:{s:02d}")
        if not attempt["submitted"] and remaining > 0:
            if HAS_REFRESH:
                st_autorefresh(interval=1000, key=f"tick-{attempt['id']}")
            else:
                time.sleep(1); st.experimental_rerun()

    qs, choices_by_q, choice_text_by_id = fetch_quiz_bundle(client, attempt["quiz_id"])
    if not qs:
        st.info("No questions available for this quiz (or you don’t have access).")
        return

    with left:
        st.subheader("Answer the questions")
        for q in qs:
            qid = q["id"]
            st.write(f"**Q{q['display_order']}. {q['body']}**")
            options = choices_by_q.get(qid, [])
            if not options:
                st.warning("No choices configured for this question.")
                st.divider()
                continue
            labels = {c["body"]: c["id"] for c in options}
            keys_list = list(labels.keys())
            chosen_id = st.session_state.get("answers", {}).get(qid)
            current_label = None
            if chosen_id:
                for k, v in labels.items():
                    if v == chosen_id:
                        current_label = k
                        break
            idx = keys_list.index(current_label) if current_label in keys_list else 0
            choice_label = st.radio(" ", options=keys_list, index=idx, key=f"radio_{qid}")
            st.session_state["answers"][qid] = labels[choice_label]
            st.divider()

        col1, col2 = st.columns([1,4])
        with col1:
            if st.button("Submit now", type="primary", disabled=attempt["submitted"] or remaining == 0):
                do_submit(client)

    # Autosubmit at 0
    if remaining == 0 and not attempt["submitted"]:
        st.warning("Time is up! Submitting your answers…")
        do_submit(client)

    # Results
    if attempt["submitted"]:
        results = st.session_state.get("results") or rpc_get_results(client, attempt["id"])
        st.session_state["results"] = results
        score = sum(1 for r in results if r["is_correct"])
        st.success(f"Your score: {score} / {len(results)}")

        # pretty print with choice text
        for r in results:
            qid = r["question_id"]
            your_id = r["chosen_choice_id"]
            corr_id = r["correct_choice_id"]
            your_txt = choice_text_by_id.get(your_id, "—")
            corr_txt = choice_text_by_id.get(corr_id, "—")
            st.write(f"**Q:** {qid}")
            st.write(f"- Your answer: {your_txt}")
            st.write(f"- Correct answer: {corr_txt} {'✅' if r['is_correct'] else '❌'}")
            if r.get("explanation"):
                with st.expander("Explanation"):
                    st.write(r["explanation"])

        colA, colB = st.columns(2)
        with colA:
            if st.button("🔁 Take this quiz again"):
                for k in ["attempt", "answers", "results"]:
                    st.session_state.pop(k, None)
                st.rerun()
        with colB:
            if st.button("🏁 Back to quiz list"):
                for k in ["attempt", "answers", "results"]:
                    st.session_state.pop(k, None)
                st.rerun()

# ---- Boot ----
client = sb_client()
auth_ui(client)
render_quiz(client)
