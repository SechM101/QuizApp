# app.py
import os
from datetime import datetime, timezone
import time
from typing import Dict, List, Optional

import streamlit as st
from supabase import create_client, Client
from dotenv import load_dotenv
from streamlit_autorefresh import st_autorefresh

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")
assert SUPABASE_URL and SUPABASE_ANON_KEY, "Set SUPABASE_URL and SUPABASE_ANON_KEY in .env"

def sb_client() -> Client:
    client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    # restore session if we have tokens
    if "session" in st.session_state:
        sess = st.session_state["session"]
        client.auth.set_session(sess["access_token"], sess["refresh_token"])
    return client

st.set_page_config(page_title="Timed Quiz", page_icon="⏱️", layout="wide")

# ------------------------- Auth UI -------------------------
def auth_ui(client: Client):
    st.sidebar.header("Account")
    if st.session_state.get("user"):
        user = st.session_state["user"]
        st.sidebar.success(f"Signed in as {user['email']}")
        if st.sidebar.button("Sign out"):
            client.auth.sign_out()
            for k in ["user", "session", "attempt", "answers", "results"]:
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
                st.success("Logged in")
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

# ------------------------- Data helpers -------------------------
def list_quizzes(client: Client):
    res = client.table("quizzes").select("id,title,description,time_limit_seconds").eq("is_published", True).order("created_at").execute()
    return res.data or []

# 1) Update the bundle fetch to log what came back
def fetch_quiz_bundle(client: Client, quiz_id: str):
    """Return (questions, choices_by_question) or ([], {}) on any issue."""
    try:
        # If you renamed `position` -> `display_order`, switch both .select/.order lines accordingly.
        qres = client.table("questions")\
            .select("id,body,explanation,position")\
            .eq("quiz_id", quiz_id)\
            .order("position")\
            .execute()
    except Exception as e:
        st.error(f"Failed to load questions: {e}")
        return [], {}

    qs = qres.data or []  # ensure list
    if not qs:
        # Gentle hint to check RLS/seed
        with st.expander("Debug (questions)"):
            st.write({"quiz_id": quiz_id, "questions_count": 0})
        return [], {}

    qids = [q["id"] for q in qs]
    try:
        cres = client.table("choices")\
            .select("id,question_id,body")\
            .in_("question_id", qids)\
            .execute()
        ch = cres.data or []
    except Exception as e:
        st.error(f"Failed to load choices: {e}")
        return qs, {}

    choices_by_q = {}
    for c in ch:
        choices_by_q.setdefault(c["question_id"], []).append(c)

    # Optional quick debug
    with st.expander("Debug (loaded)"):
        st.write({"questions": len(qs), "choices": len(ch)})

    return qs, choices_by_q
    qs, choices_by_q = fetch_quiz_bundle(client, attempt["quiz_id"])

    if not qs:
        st.info("No questions are available for this quiz (or you don’t have access). "
            "Check that the quiz is published, RLS policies allow read, and seed data exists.")
        return



def rpc_start_attempt(client: Client, quiz_id: str):
    res = client.rpc("start_attempt", {"p_quiz_id": quiz_id}).execute()
    if not res.data:
        raise RuntimeError("Failed to start attempt")
    row = res.data[0]
    return row["attempt_id"], row["started_at"], row["ends_at"]

def rpc_finish_attempt(client: Client, attempt_id: str, answers: List[Dict]):
    res = client.rpc("finish_attempt", {"p_attempt_id": attempt_id, "p_answers": answers}).execute()
    return res.data

def rpc_get_results(client: Client, attempt_id: str):
    return client.rpc("get_results", {"p_attempt_id": attempt_id}).execute().data

# ------------------------- Timer helpers -------------------------
def utcnow():
    return datetime.now(timezone.utc)

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


# ------------------------- UI -------------------------
def render_quiz(client: Client):
    st.title("⏱️ Timed Quiz")

    # Require auth
    if not st.session_state.get("user"):
        st.info("Please login on the left to take a quiz.")
        return

        # Choose or show current attempt
        quizzes = list_quizzes(client)
        if not quizzes:
            st.warning("No published quizzes yet.")
        return

        if "attempt" not in st.session_state:
        # Selection
            q_options = {f"{q['title']} ({q['time_limit_seconds']}s)": q["id"] for q in quizzes}
            label = st.selectbox("Choose a quiz", list(q_options.keys()))
        if st.button("Start quiz"):
        # defensive cleanup
        for k in ["attempt", "answers", "results"]:
            st.session_state.pop(k, None)
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

    # Active attempt
    attempt = st.session_state["attempt"]
    left_col, right_col = st.columns([2,1])

    with right_col:
        # Timer (server-authoritative end time)
        remaining = seconds_left(attempt["ends_at"])
        m, s = divmod(remaining, 60)
        st.metric("Time left", f"{m:02d}:{s:02d}")
        # Rerun every 1s while quiz is in progress and not submitted
        # where you currently do: st.autorefresh(interval=1000, key="tick")
        if not attempt["submitted"] and remaining > 0:
        # unique key per attempt avoids stale refresh behavior if user restarts a quiz
            st_autorefresh(interval=1000, key=f"tick-{attempt['id']}")
        if not attempt["submitted"] and remaining > 0:
            time.sleep(1)
            st.experimental_rerun()


    # Fetch quiz content once per run
    qs, choices_by_q = fetch_quiz_bundle(client, attempt["quiz_id"])

    with left_col:
        st.subheader("Answer all questions")
        # Render choices (all-on-one-page)
        for q in qs:
            qid = q["id"]
            st.write(f"**Q{q['position']}. {q['body']}**")
            # choices radios
            options = choices_by_q.get(qid, [])
            # create a mapping body->id for display
            labels = {c["body"]: c["id"] for c in options}
            current_choice = None
            if qid in st.session_state["answers"]:
                # find label by id
                for k,v in labels.items():
                    if v == st.session_state["answers"][qid]:
                        current_choice = k
                        break
            choice_label = st.radio(
                " ",
                options=list(labels.keys()),
                index=(list(labels.keys()).index(current_choice) if current_choice in labels else 0),
                key=f"radio_{qid}"
            )
            st.session_state["answers"][qid] = labels[choice_label]
            st.divider()

        # Submit handlers
        col1, col2 = st.columns([1,4])
        with col1:
            disabled = attempt["submitted"] or remaining == 0
            if st.button("Submit now", type="primary", disabled=disabled):
                do_submit(client)

    # Autosubmit when timer hits zero
    if remaining == 0 and not attempt["submitted"]:
        st.warning("Time is up! Submitting your answers…")
        do_submit(client)

    # Results view (after submit)
    if attempt["submitted"]:
        results = st.session_state.get("results", [])
        if not results:
            # fallback: fetch results
            try:
                results = rpc_get_results(client, attempt["id"])
                st.session_state["results"] = results
            except Exception as e:
                st.error(f"Failed to fetch results: {e}")
                return

    # After results are shown
    colA, colB = st.columns(2)
        with colA:
            if st.button("🔁 Take this quiz again"):
            # clear local state so we show the quiz picker
            for k in ["attempt", "answers", "results"]:
                st.session_state.pop(k, None)
            st.rerun()
    with colB:
        if st.button("🏁 Back to quiz list"):
            for k in ["attempt", "answers", "results"]:
                st.session_state.pop(k, None)
            st.rerun()


        # Compute score locally for display (server already set score)
        score = sum(1 for r in results if r["is_correct"])
        st.success(f"Your score: {score} / {len(results)}")

        for r in results:
            st.write(f"**Question**: {r['question_id']}")
            st.write(f"- Your answer: `{r['chosen_choice_id']}`")
            st.write(f"- Correct answer: `{r['correct_choice_id']}`")
            st.write(f"- Correct? {'✅' if r['is_correct'] else '❌'}")
            if r.get("explanation"):
                with st.expander("Explanation"):
                    st.write(r["explanation"])

def do_submit(client: Client):
    """Submit answers atomically via RPC. Protects against double submits."""
    attempt = st.session_state["attempt"]
    if attempt["submitted"]:
        return
    # build answers array for RPC
    answers = []
    for qid, cid in (st.session_state.get("answers") or {}).items():
        answers.append({"question_id": qid, "chosen_choice_id": cid})

    try:
        data = rpc_finish_attempt(client, attempt["id"], answers)
        st.session_state["results"] = data
        attempt["submitted"] = True
        st.session_state["attempt"] = attempt
        st.success("Submitted!")
        st.rerun()
    except Exception as e:
        msg = str(e)
        if "already finished" in msg:
            # benign race: one of the paths finished first
            attempt["submitted"] = True
            st.session_state["attempt"] = attempt
            st.info("Attempt was already finished.")
            st.rerun()
        else:
            st.error(f"Submit failed: {e}")

# ------------------------- Main -------------------------
client = sb_client()
auth_ui(client)
render_quiz(client)

