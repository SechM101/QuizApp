import time, random, math
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from supabase import create_client, Client
from datetime import datetime, timezone

# ---------- Config ----------
st.set_page_config(page_title="IFRS Quiz", page_icon="📚", layout="centered")

SUPABASE_URL = st.secrets["SUPABASE_URL"]
SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
DEFAULT_TIME_LIMIT = int(st.secrets.get("TIME_LIMIT_SECONDS", 120))

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------- UI: Header ----------
st.title("📚 IFRS Knowledge Quiz")
st.caption("Topics: IFRS 15, IFRS 16 (Leases), Accounting Policies")

# ---------- Inputs ----------
user_email = st.text_input("Your email (to track attempts)", value="", help="Optional for MVP; add Supabase Auth later for verified identity.")

topics = st.multiselect(
    "Choose topics",
    options=["IFRS 15", "IFRS 16", "Accounting Policies"],
    default=["IFRS 15", "IFRS 16", "Accounting Policies"]
)

num_q = st.slider("Number of questions", min_value=3, max_value=20, value=5, step=1)
time_limit = st.slider("Time limit (seconds)", min_value=30, max_value=900, value=DEFAULT_TIME_LIMIT, step=30)

# ---------- Session State ----------
if "quiz_started" not in st.session_state:
    st.session_state.quiz_started = False
if "questions" not in st.session_state:
    st.session_state.questions = []
if "deadline" not in st.session_state:
    st.session_state.deadline = None
if "answers" not in st.session_state:
    st.session_state.answers = {}  # q_index -> chosen option
if "submitted" not in st.session_state:
    st.session_state.submitted = False
if "duration_sec" not in st.session_state:
    st.session_state.duration_sec = 0

def start_quiz():
    if not topics:
        st.warning("Please select at least one topic.")
        return
    # fetch questions from Supabase
    resp = supabase.table("questions").select("*").in_("topic", topics).execute()
    rows = resp.data or []
    if not rows:
        st.error("No questions found for selected topics.")
        return
    sample_size = min(num_q, len(rows))
    sample = random.sample(rows, sample_size)

    st.session_state.questions = sample
    st.session_state.answers = {}
    st.session_state.quiz_started = True
    st.session_state.submitted = False
    now = time.time()
    st.session_state.deadline = now + time_limit
    st.session_state.duration_sec = 0

if st.button("Start / Reset Quiz"):
    start_quiz()

if not st.session_state.quiz_started:
    st.info("Click **Start / Reset Quiz** to begin.")
    st.stop()

# ---------- Timer (auto-refresh every 1s) ----------
st_autorefresh(interval=1000, key="tick")  # rerun every second

remaining = max(0, int(st.session_state.deadline - time.time()))
elapsed = time_limit - remaining
st.session_state.duration_sec = elapsed

# Visual timer
col1, col2 = st.columns(2)
with col1:
    st.metric("Time remaining (sec)", remaining)
with col2:
    st.progress(remaining / time_limit)

if remaining <= 0 and not st.session_state.submitted:
    st.warning("⏰ Time's up! Auto-submitting your answers...")
    st.session_state.submitted = True

# ---------- Render questions ----------
st.subheader("Questions")
for idx, q in enumerate(st.session_state.questions):
    st.markdown(f"**{idx+1}. {q['text']}**")
    choices = q["choices"]
    # preserve a stable shuffle per session
    rnd = random.Random(str(q["id"]))
    shuffled = choices[:]
    rnd.shuffle(shuffled)

    def on_change(i=idx):
        st.session_state.answers[i] = st.session_state.get(f"sel_{i}")

    st.radio(
        "Select one:",
        options=shuffled,
        key=f"sel_{idx}",
        index=None if idx not in st.session_state.answers else shuffled.index(st.session_state.answers[idx]),
        on_change=on_change
    )
    st.divider()

# ---------- Submit button (or auto-submission on timeout) ----------
if st.button("Submit Answers") and not st.session_state.submitted:
    st.session_state.submitted = True

if not st.session_state.submitted:
    st.stop()

# ---------- Scoring & Feedback ----------
details = []
correct_count = 0
for idx, q in enumerate(st.session_state.questions):
    chosen = st.session_state.answers.get(idx, None)
    is_correct = (chosen == q["answer"])
    if is_correct:
        correct_count += 1
    details.append({
        "question_id": q["id"],
        "topic": q["topic"],
        "question": q["text"],
        "chosen": chosen,
        "correct_answer": q["answer"],
        "correct": is_correct,
        "explanation": q.get("explanation") or ""
    })

total = len(st.session_state.questions)
score_pct = round((correct_count / total) * 100, 1)

# Store in Supabase
result_doc = {
    "user_email": user_email or None,
    "topics": [q["topic"] for q in st.session_state.questions],
    "total": total,
    "correct": correct_count,
    "score_pct": score_pct,
    "answers": details,
    "duration_sec": int(st.session_state.duration_sec)
}
supabase.table("quiz_results").insert(result_doc).execute()

# Show summary
st.success(f"Score: {correct_count}/{total} ({score_pct}%). Time taken: {int(st.session_state.duration_sec)} sec")

# Show per-question feedback (what was wrong + correct answer)
st.subheader("Your answers & explanations")
for d in details:
    icon = "✅" if d["correct"] else "❌"
    st.markdown(f"{icon} **Q:** {d['question']}")
    st.write(f"- **Your answer:** {d['chosen']}")
    st.write(f"- **Correct answer:** {d['correct_answer']}")
    if d["explanation"]:
        st.write(f"- _Why:_ {d['explanation']}")
    st.write("---")
