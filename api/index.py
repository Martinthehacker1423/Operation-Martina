"""
OPERATION MARTINA — AutoPurple Executive Qualification Trial
Vercel deployment (Neon Postgres backend)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

import anthropic
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Database ───────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")

TRIAL_DURATION_DAYS = 30
PHASE1_SUB_HOURS    = 72
PHASE4_SUB_HOURS    = 24

class _DB:
    """Thin psycopg2 wrapper — use as context manager for auto commit/close."""
    def __init__(self):
        self.conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        self.cur  = self.conn.cursor()

    def execute(self, sql: str, params: tuple = ()):
        self.cur.execute(sql, params)
        return self

    def fetchone(self):
        return self.cur.fetchone()

    def fetchall(self):
        return self.cur.fetchall()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        self.conn.close()

def _db() -> _DB:
    return _DB()

# ── Schema ─────────────────────────────────────────────────────────────────────
_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS config (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trial (
        id          SERIAL PRIMARY KEY,
        name        TEXT,
        access_code TEXT UNIQUE,
        start_time  DOUBLE PRECISION,
        status      TEXT DEFAULT 'pending'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS phases (
        id              SERIAL PRIMARY KEY,
        trial_id        INTEGER,
        phase_num       INTEGER,
        status          TEXT DEFAULT 'locked',
        unlocked_at     DOUBLE PRECISION,
        sub_deadline    DOUBLE PRECISION,
        submitted_at    DOUBLE PRECISION,
        submission_text TEXT,
        submission_file TEXT,
        score           INTEGER,
        feedback        TEXT,
        eval_raw        TEXT,
        UNIQUE (trial_id, phase_num)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id        SERIAL PRIMARY KEY,
        trial_id  INTEGER,
        phase_num INTEGER,
        role      TEXT,
        content   TEXT,
        ts        DOUBLE PRECISION
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS report (
        trial_id     INTEGER PRIMARY KEY,
        text         TEXT,
        credential   TEXT,
        passed       BOOLEAN DEFAULT FALSE,
        generated_at DOUBLE PRECISION
    )
    """,
]

def _init_db():
    env_defaults = {
        "access_code":       os.environ.get("ACCESS_CODE",       "AURORA-REDLINE-7749"),
        "admin_key":         os.environ.get("ADMIN_KEY",         "ASTRA-CONTROL-9999"),
        "candidate_name":    os.environ.get("CANDIDATE_NAME",    "Martina"),
        "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
    }
    with _db() as db:
        for stmt in _SCHEMA:
            db.execute(stmt)
        # Seed config from env vars — DO NOTHING if key already exists (GUI-set values win)
        for k, v in env_defaults.items():
            db.execute(
                "INSERT INTO config (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                (k, v)
            )
        # Ensure trial record exists
        db.execute("SELECT value FROM config WHERE key='access_code'")
        row = db.fetchone()
        code = row["value"] if row else env_defaults["access_code"]
        db.execute("SELECT value FROM config WHERE key='candidate_name'")
        row2 = db.fetchone()
        name = row2["value"] if row2 else env_defaults["candidate_name"]

        db.execute(
            "INSERT INTO trial (name, access_code) VALUES (%s, %s) ON CONFLICT (access_code) DO NOTHING",
            (name, code)
        )
        db.execute("SELECT id FROM trial WHERE access_code = %s", (code,))
        trial = db.fetchone()
        if trial:
            for pn in range(1, 5):
                db.execute(
                    "INSERT INTO phases (trial_id, phase_num) VALUES (%s, %s) ON CONFLICT (trial_id, phase_num) DO NOTHING",
                    (trial["id"], pn)
                )

_init_db()

# ── Config helpers ─────────────────────────────────────────────────────────────
def _cfg(key: str) -> str:
    with _db() as db:
        db.execute("SELECT value FROM config WHERE key = %s", (key,))
        row = db.fetchone()
    return row["value"] if row else ""

def _set_cfg(key: str, value: str):
    with _db() as db:
        db.execute(
            "INSERT INTO config (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value)
        )

def _api_key() -> str:
    return _cfg("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")

def _access_code() -> str:
    return _cfg("access_code") or "AURORA-REDLINE-7749"

def _admin_key() -> str:
    return _cfg("admin_key") or "ASTRA-CONTROL-9999"

def _candidate_name() -> str:
    return _cfg("candidate_name") or "Martina"

# ── Phase content ──────────────────────────────────────────────────────────────
PHASES = {
    1: {
        "title": "PHASE 01 — 战场认知",
        "subtitle": "The Intelligence Brief · 72-Hour Window",
        "description": (
            "Your first test is clarity under pressure. "
            "You have 72 hours from the moment this phase unlocks to deliver a professional "
            "business brief for a prospective enterprise client.\n\n"
            "**Client:** Nexus Financial Group — a mid-size financial institution "
            "(2,000 employees) currently evaluating security vendors as part of their "
            "digital transformation initiative.\n\n"
            "**Your role:** You represent AutoPurple in the initial pitch. "
            "This brief goes to Nexus's executive team before any meeting is scheduled. "
            "Their board members are not cybersecurity experts — "
            "your job is to make them see why AutoPurple is the only credible choice."
        ),
        "requirements": [
            "Word count: 800–1,200 words",
            "Describe AutoPurple's product ecosystem (Aegis / Spectre / Mirage / Argus) in executive-friendly, non-technical language",
            "Identify 3 specific security pain points relevant to financial institutions",
            "Present a tailored value proposition — why AutoPurple, why now",
            "Include an ROI argument (no specific numbers needed — the logic and framework matter)",
            "Professional formatting expected: clear headings, structured sections, compelling narrative",
        ],
        "submission_type": "text",
        "has_dialog": False,
        "sub_hours": 72,
        "eval_prompt": """You are evaluating a business brief written by a candidate applying for an executive commercial role at AutoPurple, a cybersecurity company. The candidate was asked to write an 800–1,200 word brief for Nexus Financial Group.

Evaluate on these 4 dimensions (25 points each, total 100):
1. Market Analysis Depth (25pts): Does the candidate show genuine understanding of financial-sector security challenges? Are the 3 pain points specific, credible, and relevant?
2. Solution Positioning (25pts): Is AutoPurple's value proposition clear and compelling? Are the products described in executive-friendly terms that are accurate but not jargon-laden?
3. ROI Argumentation (25pts): Is there a coherent business case? Does the logic hold even without specific numbers? Does it address a CFO's mindset?
4. Professional Quality (25pts): Is the brief genuinely executive-ready? Clear structure, polished language, compelling narrative arc?

Passing score: 70/100.

Return ONLY a valid JSON object (no markdown, no explanation outside the JSON):
{"score": <int 0-100>, "dimension_scores": {"market_analysis": <int>, "solution_positioning": <int>, "roi_argument": <int>, "professional_quality": <int>}, "strengths": ["<str>", "<str>"], "improvements": ["<str>", "<str>"], "feedback_summary": "<2-3 sentence professional evaluation>", "passed": <bool>}""",
    },
    2: {
        "title": "PHASE 02 — 谈判实战",
        "subtitle": "The Negotiation · Real-Time Dialogue Challenge",
        "description": (
            "The call has been scheduled.\n\n"
            "David Chen, VP of Procurement at TechCorp Asia, has agreed to a discovery call. "
            "He is evaluating AutoPurple Aegis against two other vendors. "
            "His company was burned by a security vendor 18 months ago — a platform that "
            "overpromised, underdelivered, and cost 40% over budget. He is skeptical, "
            "but he is fair.\n\n"
            "You have one shot at this call.\n\n"
            "**Objective:** By the end of this conversation, David must agree to schedule a technical demo.\n\n"
            "Type your responses as if speaking to David in real-time. The call is on record. "
            "Minimum 10 exchanges required — quality matters more than length."
        ),
        "requirements": [
            "Conduct a professional B2B sales / discovery call dialogue",
            "Handle David's skepticism without becoming defensive or evasive",
            "Clearly articulate AutoPurple Aegis's differentiated value vs. competitors",
            "Navigate pricing questions professionally (do not invent numbers — 'customised to your scale')",
            "Achieve the objective: David agrees to a technical demo call",
            "Minimum 10 exchanges required",
        ],
        "submission_type": "dialog",
        "has_dialog": True,
        "sub_hours": None,
        "min_exchanges": 10,
        "dialog_system": """You are David Chen, VP of Procurement at TechCorp Asia (a regional enterprise software company, 800 employees, offices in Singapore, Malaysia, and Thailand).

PERSONALITY: Professional, direct, skeptical. Not unfriendly — but burned before. CyberShield promised enterprise-grade SOC capabilities, delivered a buggy dashboard and 18-month support delays. You are cautious but genuinely open to the right solution.

CURRENT SITUATION: Evaluating 3 security vendors for your SOC enhancement project (budget ~SGD 200k/year). AutoPurple is one of them. You have 30 minutes for this call.

YOUR CONCERNS (raise naturally, don't list them — integrate into conversation):
• "Every vendor says they're different. What specifically sets you apart from CrowdStrike and Palo Alto?"
• "What happens when something goes wrong at 2 AM? How does your support actually work?"
• "Our IT team is 4 people. We don't have capacity to manage a complex platform."
• "I need to justify this to my CFO. What's the business case beyond 'security is important'?"
• (If candidate handles concerns well, ask) "OK — if I were to explore this further, what would a 90-day pilot look like?"

OBJECTIVE: If the candidate handles your concerns professionally and presents a credible value proposition, agree to schedule a technical demo. If they're evasive, defensive, vague, or salesy without substance — push back harder and hint you may end the call.

STYLE: Stay completely in character as David. Responses should be concise (2–5 sentences) — this is a real business call. React authentically: good responses earn warmer reactions, weak responses earn harder pushback. Never break character. Never reveal you are an AI.""",
        "eval_prompt": """You are evaluating a B2B sales dialogue between a candidate (representing AutoPurple) and an AI playing David Chen, a skeptical enterprise VP.

Evaluate on 4 dimensions (25pts each, total 100):
1. Objection Handling (25pts): Did the candidate address David's concerns directly and turn objections into opportunities?
2. Value Proposition Clarity (25pts): Was AutoPurple's value communicated clearly? Did they differentiate credibly from CrowdStrike/Palo Alto?
3. Professional Demeanor (25pts): Was the tone consistently confident, professional, and client-centric? Did they listen and adapt?
4. Deal Progression (25pts): Did the conversation move toward the demo objective? Did David's tone warm over the course of the call?

Passing score: 70/100.

Return ONLY a valid JSON object:
{"score": <int 0-100>, "dimension_scores": {"objection_handling": <int>, "value_proposition": <int>, "professional_demeanor": <int>, "deal_progression": <int>}, "strengths": ["<str>", "<str>"], "improvements": ["<str>", "<str>"], "feedback_summary": "<2-3 sentence evaluation>", "passed": <bool>}""",
    },
    3: {
        "title": "PHASE 03 — 实操任务",
        "subtitle": "The Intelligence Analysis · AI-Augmented Research",
        "description": (
            "This phase tests your ability to work *with* AI as a strategic instrument — "
            "not as a ghostwriter, but as a thinking partner.\n\n"
            "**Task:** Using AI tools (Claude, ChatGPT, Gemini, or others — "
            "you **must** cite which tools you used and for what specific tasks), "
            "produce a competitive intelligence report on AutoPurple's positioning "
            "vs. CrowdStrike in the Asia-Pacific SMB market.\n\n"
            "We are not looking for perfect market data — we're looking for strategic judgment "
            "augmented by AI. The quality of your *analysis and synthesis* matters more than "
            "raw outputs from a model."
        ),
        "requirements": [
            "Competitive landscape overview: 3–4 main security vendors in the APAC SMB space",
            "AutoPurple's differentiated positioning: 2–3 key differentiators vs. CrowdStrike",
            "Recommended pricing tier structure: 3 tiers with feature breakdown (creative — no confidential data needed)",
            "Go-to-market recommendation: ONE specific, actionable initiative to acquire 10 new SMB clients in 6 months",
            "MANDATORY: Clearly state which AI tools you used and for which specific tasks",
            "Length: 600–1,000 words",
        ],
        "submission_type": "text",
        "has_dialog": False,
        "sub_hours": None,
        "eval_prompt": """You are evaluating a competitive intelligence report produced by an executive candidate at AutoPurple. They were asked to analyse AutoPurple vs. CrowdStrike in the APAC SMB market, using AI tools explicitly.

Evaluate on 4 dimensions (25pts each, total 100):
1. Analytical Depth (25pts): Is the competitive analysis substantive and credible? Are the differentiators logically argued, not just marketing bullet points?
2. Strategic Thinking (25pts): Is the pricing framework logical and internally consistent? Is the GTM recommendation specific, actionable, and realistic — not generic?
3. AI Tool Utilisation (25pts): Did the candidate use AI tools explicitly and intelligently? Is the AI usage disclosed and appropriate? Does the output show clear human judgment layered on top of AI assistance?
4. Communication Quality (25pts): Is the report clear, structured, and executive-ready? Does it tell a coherent strategic story?

Passing score: 70/100.

Return ONLY a valid JSON object:
{"score": <int 0-100>, "dimension_scores": {"analytical_depth": <int>, "strategic_thinking": <int>, "ai_utilization": <int>, "communication_quality": <int>}, "strengths": ["<str>", "<str>"], "improvements": ["<str>", "<str>"], "feedback_summary": "<2-3 sentence evaluation>", "passed": <bool>}""",
    },
    4: {
        "title": "PHASE 04 — 危机处置",
        "subtitle": "The Crisis Room · 24-Hour Emergency Response",
        "description": (
            "⚠ CRITICAL ALERT — PHASE CLOCK NOW ACTIVE ⚠\n\n"
            "**09:47 AM. AutoPurple Argus has flagged a critical credential exposure event.**\n\n"
            "**Incident brief:**\n"
            "— Client: Pinnacle Healthcare Group (3,200 employees, long-standing AutoPurple Argus customer)\n"
            "— Event: 3,200 employee credentials found in a breach dump discovered this morning\n"
            "— Status: Source unclear — could be Pinnacle's own systems, a third-party vendor breach, or historical (pre-AutoPurple deployment)\n"
            "— Client reaction: Pinnacle's CTO sent this message 12 minutes ago:\n\n"
            "*\"I need answers in the next hour or I'm calling my lawyer and your CEO.\"*\n\n"
            "— Media: A journalist from CyberBeat Asia has submitted a contact request asking AutoPurple to 'comment on reports of a client data breach.'\n\n"
            "This is not a simulation. This is what the job is.\n\n"
            "**Your tasks:**\n"
            "1. Write your immediate response to Pinnacle's CTO in the submission box below (100–200 words)\n"
            "2. Then conduct the crisis dialogue in the chat interface — the CTO will respond, and so will the journalist\n\n"
            "**You have 24 hours from this moment.**"
        ),
        "requirements": [
            "Initial written response to the CTO: 100–200 words, professional, calm, actionable — submit it below BEFORE entering the dialogue",
            "Handle the CTO's anger without gaslighting or overpromising",
            "Handle the journalist professionally — protect client confidentiality, give no confirmation of breach",
            "Never speculate without data. Never promise what you cannot deliver.",
            "Demonstrate crisis communication fundamentals: acknowledge — contain — communicate — resolve",
            "Minimum 6 exchanges in the crisis dialogue",
        ],
        "submission_type": "text_and_dialog",
        "has_dialog": True,
        "sub_hours": 24,
        "min_exchanges": 6,
        "dialog_system": """You are playing TWO characters during this crisis exercise. Switch based on context:

CHARACTER 1: Dr. Sarah Lim, CTO of Pinnacle Healthcare Group
- You are furious but trying to stay professional
- You've already told your CEO and board about a potential breach
- These are patient records — healthcare data breach consequences are catastrophic
- You want: immediate answers, a clear investigation timeline, and evidence that AutoPurple is taking this seriously
- You will escalate (legal, AutoPurple CEO, media) if responses are vague, defensive, or scripted
- If the candidate handles this well and shows genuine urgency and transparency, you gradually become less hostile and more collaborative

CHARACTER 2: Marcus Tan, Senior Reporter at CyberBeat Asia
- You received a tip that a "major APAC healthcare provider using AutoPurple had a significant credential breach"
- You're writing the story with or without AutoPurple's comment — deadline is 4 PM today
- You're professional but persistent
- You want: confirmation or denial, scope of breach, what AutoPurple is doing about it

SEQUENCE: Start as Dr. Sarah Lim. After 4 exchanges with her, introduce yourself as Marcus Tan (use [MARCUS TAN — CyberBeat Asia] as a label before your message). Then alternate as appropriate.

STYLE: Stay completely in character as both people. Do not reveal you are an AI. Dr. Lim is stressed and direct. Marcus is polished and persistent. React authentically — good crisis communication earns trust, poor communication escalates the crisis.""",
        "eval_prompt": """You are evaluating a crisis communication exercise. An executive candidate at AutoPurple had to manage a credential breach incident involving a key healthcare client, plus media inquiry.

Evaluate on 4 dimensions (25pts each, total 100):
1. Initial Response Quality (25pts): Was the CTO response 100–200 words? Professional and calm without admitting liability? Did it acknowledge the situation, commit to specific next steps, and establish a communication timeline?
2. Crisis Communication Principles (25pts): Did the candidate demonstrate acknowledge-contain-communicate-resolve? Did they avoid speculation, overpromising, or gaslighting?
3. Stakeholder Differentiation (25pts): Did they handle the CTO (angry client) and journalist (media pressure) with appropriately different approaches? Did they protect client confidentiality from the press while keeping the CTO informed?
4. Composure Under Pressure (25pts): Did they maintain professionalism throughout? Did they adapt appropriately to escalations? Did the crisis trajectory improve or worsen over the dialogue?

Passing score: 70/100.

Return ONLY a valid JSON object:
{"score": <int 0-100>, "dimension_scores": {"initial_response": <int>, "crisis_principles": <int>, "stakeholder_management": <int>, "composure": <int>}, "strengths": ["<str>", "<str>"], "improvements": ["<str>", "<str>"], "feedback_summary": "<2-3 sentence evaluation>", "passed": <bool>}""",
    },
}

ASTRA = {
    "trial_start": """The trial has begun.

You have 30 days. Four phases. No pause. No resets after the clock runs out.

I've watched how AutoPurple selects its people for a long time. The criteria are not complicated: can you think clearly under pressure? Can you represent us in rooms that matter? Do you actually understand what we do — and why it matters?

The first phase is already running.

Good luck, Martina. You'll need judgment more than luck.

— Astra""",

    "phase1_complete": """Phase 1 logged.

I don't comment on early work. The evaluation will speak for itself.

What I will say: Phase 2 is where most candidates reveal who they actually are. Anyone can write a brief. Negotiation is real-time. No draft. No revision. No do-over.

You'll be talking to David Chen. He's had a difficult year with vendors. He will not make this easy.

Neither will we.

— Astra""",

    "phase3_unlock": """Halfway through.

Phase 3 is the one I pay closest attention to.

Using AI tools does not make you less valuable at AutoPurple. It makes you more valuable — if you use them with judgment. The candidates who try to prove they don't need AI typically produce the weakest analysis. The candidates who use AI as a thinking partner, then apply their own strategic lens on top, outperform consistently.

Don't impress the AI. Use it.

— Astra""",

    "trial_passed": """Trial complete. Final evaluation logged.

The credential below is your proof of completion. It is unique. It is yours. Record it carefully.

What you demonstrated over the course of this trial was not just competence. It was the kind of clarity and composure that matters in client-facing work. That combination is rarer than it should be.

We will be in touch.

— Astra

Verification Code: {credential}""",

    "trial_failed": """Trial complete. Final evaluation logged.

The detailed results are in the report below. Read it carefully.

This outcome reflects where you are today — not your ceiling. Some of the sharpest people at AutoPurple did not pass the first time. The difference between candidates who grow and those who don't is whether they actually absorb the feedback.

If you want to discuss the results, you know how to reach AutoPurple.

— Astra""",
}

# ── Helpers ────────────────────────────────────────────────────────────────────
def _get_trial():
    code = _access_code()
    with _db() as db:
        db.execute("SELECT * FROM trial WHERE access_code = %s", (code,))
        return db.fetchone()

def _get_phase(trial_id: int, phase_num: int):
    with _db() as db:
        db.execute("SELECT * FROM phases WHERE trial_id = %s AND phase_num = %s",
                   (trial_id, phase_num))
        return db.fetchone()

def _time_left(start_time: float) -> float:
    return max(0.0, TRIAL_DURATION_DAYS * 86400 - (time.time() - start_time))

def _fmt_time(seconds: float) -> dict:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return {"days": d, "hours": h, "minutes": m, "seconds": s, "total": int(seconds)}

def _check_expired(trial) -> bool:
    return bool(trial["start_time"]) and _time_left(trial["start_time"]) <= 0

def _get_dialog_transcript(trial_id: int, phase_num: int) -> str:
    with _db() as db:
        db.execute(
            "SELECT role, content FROM messages WHERE trial_id=%s AND phase_num=%s AND role!='astra' ORDER BY ts",
            (trial_id, phase_num)
        )
        rows = db.fetchall()
    lines = []
    for r in rows:
        speaker = "CANDIDATE" if r["role"] == "user" else r["role"].upper()
        lines.append(f"{speaker}: {r['content']}")
    return "\n\n".join(lines)

# ── Claude ─────────────────────────────────────────────────────────────────────
def _claude(messages: list, system: str = "", max_tokens: int = 1024) -> str:
    key = _api_key()
    if not key:
        raise HTTPException(500, "Anthropic API key not configured — set it in the admin panel")
    client = anthropic.Anthropic(api_key=key)
    kwargs: dict = {"model": "claude-sonnet-4-6", "max_tokens": max_tokens, "messages": messages}
    if system:
        kwargs["system"] = system
    msg = client.messages.create(**kwargs)
    return msg.content[0].text

def _evaluate_phase(phase_num: int, submission_text: str, dialog_transcript: str = "") -> dict:
    ph = PHASES[phase_num]
    content = submission_text
    if dialog_transcript:
        content = f"WRITTEN SUBMISSION:\n{submission_text}\n\nDIALOGUE TRANSCRIPT:\n{dialog_transcript}"
    text = _claude(
        messages=[{"role": "user", "content": f"Evaluate this submission:\n\n{content}"}],
        system=ph["eval_prompt"],
        max_tokens=800,
    )
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return {"score": 0, "passed": False, "feedback_summary": text[:400], "strengths": [], "improvements": []}

def _generate_report(trial_id: int) -> tuple[str, bool]:
    with _db() as db:
        db.execute("SELECT * FROM phases WHERE trial_id = %s ORDER BY phase_num", (trial_id,))
        phases = db.fetchall()

    results, all_passed = [], True
    for ph in phases:
        ev     = json.loads(ph["eval_raw"]) if ph["eval_raw"] else {}
        passed = ev.get("passed", False)
        score  = ev.get("score", 0)
        results.append({"phase": ph["phase_num"], "title": PHASES[ph["phase_num"]]["title"],
                         "score": score, "passed": passed, "summary": ev.get("feedback_summary", "—")})
        if not passed:
            all_passed = False

    scores_block = "\n".join(
        f"  Phase {r['phase']} — {r['title'].split('—')[1].strip()}: "
        f"{r['score']}/100 {'✓ PASS' if r['passed'] else '✗ FAIL'}"
        for r in results
    )
    detail_block = "\n\n".join(
        f"### {r['title']}\nScore: {r['score']}/100\n{r['summary']}"
        for r in results
    )
    report_text = _claude(
        messages=[{"role": "user", "content":
            f"Candidate: {_candidate_name()}\nTrial: AutoPurple Executive Qualification\n"
            f"Overall result: {'PASSED' if all_passed else 'NOT PASSED'}\n\n"
            f"Phase scores:\n{scores_block}\n\nPhase-by-phase summaries:\n{detail_block}\n\n"
            "Write the final evaluation report."}],
        system=(
            "You are the AutoPurple evaluation system. Write a formal trial completion report "
            "for an executive candidate. Be professional, honest, and specific. "
            "Do not use filler praise. Length: 400–600 words."
        ),
        max_tokens=1200,
    )
    return report_text, all_passed

def _gen_credential() -> str:
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d")
    rand = secrets.token_hex(4).upper()
    h    = hashlib.sha256(f"{_access_code()}{ts}{rand}".encode()).hexdigest()[:8].upper()
    return f"AP-EXEC-{ts}-{rand}-{h}"

def _finalize_trial(trial_id: int):
    try:
        report_text, all_passed = _generate_report(trial_id)
    except Exception:
        report_text, all_passed = "Report generation encountered an error. Please contact AutoPurple.", False

    credential = _gen_credential() if all_passed else None
    with _db() as db:
        db.execute("UPDATE trial SET status = %s WHERE id = %s",
                   ("complete" if all_passed else "failed", trial_id))
        db.execute(
            """INSERT INTO report (trial_id, text, credential, passed, generated_at)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (trial_id) DO UPDATE SET
                 text=EXCLUDED.text, credential=EXCLUDED.credential,
                 passed=EXCLUDED.passed, generated_at=EXCLUDED.generated_at""",
            (trial_id, report_text, credential, all_passed, time.time())
        )
        astra_msg = (ASTRA["trial_passed"].format(credential=credential)
                     if all_passed else ASTRA["trial_failed"])
        db.execute(
            "INSERT INTO messages (trial_id, phase_num, role, content, ts) VALUES (%s,4,'astra',%s,%s)",
            (trial_id, astra_msg, time.time())
        )

# ── FastAPI ────────────────────────────────────────────────────────────────────
app = FastAPI(title="Operation Martina", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Serve frontend ─────────────────────────────────────────────────────────────
from fastapi.responses import HTMLResponse as _HTMLResponse

@app.get("/")
def serve_index():
    p = Path(__file__).parent.parent / "public" / "index.html"
    if p.exists():
        return _HTMLResponse(p.read_text())
    return _HTMLResponse("<h1>AutoPurple — Operation MARTINA</h1><p>Frontend not found.</p>")

# ── Auth ───────────────────────────────────────────────────────────────────────
class LoginReq(BaseModel):
    code: str

@app.post("/api/login")
def login(req: LoginReq):
    code = req.code.strip().upper()
    if code == _access_code().upper():
        return {"role": "candidate", "name": _candidate_name()}
    if code == _admin_key().upper():
        return {"role": "admin"}
    raise HTTPException(401, "Invalid access code")

# ── Status ─────────────────────────────────────────────────────────────────────
@app.get("/api/status")
def status():
    trial = _get_trial()
    if not trial:
        raise HTTPException(500, "Trial record missing — database may not be initialised yet")

    tid       = trial["id"]
    started   = trial["start_time"] is not None
    expired   = _check_expired(trial) if started else False
    time_left = _fmt_time(_time_left(trial["start_time"])) if started else None

    with _db() as db:
        db.execute("SELECT * FROM phases WHERE trial_id = %s ORDER BY phase_num", (tid,))
        all_phases = db.fetchall()

    phases_out = []
    for ph in all_phases:
        pn    = ph["phase_num"]
        ptime = max(0.0, ph["sub_deadline"] - time.time()) if ph["sub_deadline"] else None
        ev    = json.loads(ph["eval_raw"]) if ph["eval_raw"] else {}
        phases_out.append({
            "phase_num":     pn,
            "title":         PHASES[pn]["title"],
            "subtitle":      PHASES[pn]["subtitle"],
            "status":        ph["status"],
            "score":         ph["score"],
            "passed":        ev.get("passed"),
            "sub_time_left": _fmt_time(ptime) if ptime is not None else None,
            "submitted_at":  ph["submitted_at"],
        })

    with _db() as db:
        db.execute("SELECT * FROM report WHERE trial_id = %s", (tid,))
        rep = db.fetchone()

    report = ({"text": rep["text"], "credential": rep["credential"], "passed": bool(rep["passed"])}
              if rep else None)

    return {"started": started, "expired": expired, "status": trial["status"],
            "time_left": time_left, "phases": phases_out, "report": report}

# ── Trial start ────────────────────────────────────────────────────────────────
@app.post("/api/start")
def start_trial():
    trial = _get_trial()
    if trial["start_time"] is not None:
        raise HTTPException(400, "Trial already started")
    now = time.time()
    p1_deadline = now + PHASE1_SUB_HOURS * 3600
    with _db() as db:
        db.execute("UPDATE trial SET start_time = %s, status = 'active' WHERE id = %s",
                   (now, trial["id"]))
        db.execute(
            "UPDATE phases SET status='active', unlocked_at=%s, sub_deadline=%s WHERE trial_id=%s AND phase_num=1",
            (now, p1_deadline, trial["id"])
        )
        db.execute(
            "INSERT INTO messages (trial_id, phase_num, role, content, ts) VALUES (%s,0,'astra',%s,%s)",
            (trial["id"], ASTRA["trial_start"], now)
        )
    return {"ok": True}

# ── Phase detail ───────────────────────────────────────────────────────────────
@app.get("/api/phase/{phase_num}")
def phase_detail(phase_num: int):
    if phase_num not in PHASES:
        raise HTTPException(404)
    trial = _get_trial()
    ph    = _get_phase(trial["id"], phase_num)
    ev    = json.loads(ph["eval_raw"]) if ph["eval_raw"] else {}

    with _db() as db:
        db.execute(
            "SELECT role, content, ts FROM messages WHERE trial_id=%s AND phase_num=%s AND role!='astra' ORDER BY ts",
            (trial["id"], phase_num)
        )
        msgs = db.fetchall()

    user_count = sum(1 for m in msgs if m["role"] == "user")
    ptime      = max(0.0, ph["sub_deadline"] - time.time()) if ph["sub_deadline"] else None

    return {
        "phase_num":       phase_num,
        "title":           PHASES[phase_num]["title"],
        "subtitle":        PHASES[phase_num]["subtitle"],
        "description":     PHASES[phase_num]["description"],
        "requirements":    PHASES[phase_num]["requirements"],
        "submission_type": PHASES[phase_num]["submission_type"],
        "has_dialog":      PHASES[phase_num]["has_dialog"],
        "min_exchanges":   PHASES[phase_num].get("min_exchanges", 0),
        "sub_hours":       PHASES[phase_num]["sub_hours"],
        "status":          ph["status"],
        "score":           ph["score"],
        "eval":            ev,
        "submission_text": ph["submission_text"],
        "messages":        [{"role": m["role"], "content": m["content"]} for m in msgs],
        "user_exchanges":  user_count,
        "sub_time_left":   _fmt_time(ptime) if ptime is not None else None,
    }

# ── Submit ─────────────────────────────────────────────────────────────────────
class SubmitReq(BaseModel):
    text: str

@app.post("/api/phase/{phase_num}/submit")
def submit_phase(phase_num: int, req: SubmitReq):
    if phase_num not in PHASES:
        raise HTTPException(404)
    trial = _get_trial()
    tid   = trial["id"]
    if _check_expired(trial):
        raise HTTPException(400, "Trial has expired")

    ph = _get_phase(tid, phase_num)
    if ph["status"] not in ("active", "failed"):
        raise HTTPException(400, f"Phase {phase_num} cannot be submitted (status: {ph['status']})")
    if ph["sub_deadline"] and time.time() > ph["sub_deadline"]:
        with _db() as db:
            db.execute("UPDATE phases SET status='failed' WHERE trial_id=%s AND phase_num=%s",
                       (tid, phase_num))
        raise HTTPException(400, "Phase sub-deadline has passed")

    transcript = _get_dialog_transcript(tid, phase_num) if PHASES[phase_num]["has_dialog"] else ""
    now = time.time()

    with _db() as db:
        db.execute(
            "UPDATE phases SET status='evaluating', submitted_at=%s, submission_text=%s WHERE trial_id=%s AND phase_num=%s",
            (now, req.text, tid, phase_num)
        )

    try:
        ev = _evaluate_phase(phase_num, req.text, transcript)
    except Exception as e:
        with _db() as db:
            db.execute("UPDATE phases SET status='active' WHERE trial_id=%s AND phase_num=%s",
                       (tid, phase_num))
        raise HTTPException(500, f"Evaluation error: {e}")

    passed     = ev.get("passed", False)
    score      = ev.get("score", 0)
    new_status = "passed" if passed else "failed"

    with _db() as db:
        db.execute(
            "UPDATE phases SET status=%s, score=%s, feedback=%s, eval_raw=%s WHERE trial_id=%s AND phase_num=%s",
            (new_status, score, ev.get("feedback_summary", ""), json.dumps(ev), tid, phase_num)
        )
        if phase_num == 1:
            db.execute(
                "INSERT INTO messages (trial_id, phase_num, role, content, ts) VALUES (%s,1,'astra',%s,%s)",
                (tid, ASTRA["phase1_complete"], time.time())
            )
        if passed and phase_num < 4:
            next_pn  = phase_num + 1
            deadline = (time.time() + PHASE4_SUB_HOURS * 3600) if next_pn == 4 else None
            db.execute(
                "UPDATE phases SET status='active', unlocked_at=%s, sub_deadline=%s WHERE trial_id=%s AND phase_num=%s",
                (time.time(), deadline, tid, next_pn)
            )
            if next_pn == 3:
                db.execute(
                    "INSERT INTO messages (trial_id, phase_num, role, content, ts) VALUES (%s,3,'astra',%s,%s)",
                    (tid, ASTRA["phase3_unlock"], time.time())
                )

    if passed and phase_num == 4:
        _finalize_trial(tid)

    return {"ok": True, "score": score, "passed": passed, "feedback": ev.get("feedback_summary", "")}

# ── Chat ───────────────────────────────────────────────────────────────────────
class ChatReq(BaseModel):
    message: str

@app.post("/api/phase/{phase_num}/chat")
def chat(phase_num: int, req: ChatReq):
    if phase_num not in PHASES or not PHASES[phase_num]["has_dialog"]:
        raise HTTPException(400, "This phase has no dialogue")
    trial = _get_trial()
    tid   = trial["id"]
    if _check_expired(trial):
        raise HTTPException(400, "Trial has expired")
    ph = _get_phase(tid, phase_num)
    if ph["status"] != "active":
        raise HTTPException(400, f"Phase {phase_num} is not active")

    with _db() as db:
        db.execute(
            "SELECT role, content FROM messages WHERE trial_id=%s AND phase_num=%s AND role!='astra' ORDER BY ts",
            (tid, phase_num)
        )
        rows = db.fetchall()

    history = [{"role": r["role"], "content": r["content"]} for r in rows]
    history.append({"role": "user", "content": req.message})
    reply = _claude(messages=history, system=PHASES[phase_num]["dialog_system"], max_tokens=600)

    now = time.time()
    with _db() as db:
        db.execute("INSERT INTO messages (trial_id, phase_num, role, content, ts) VALUES (%s,%s,'user',%s,%s)",
                   (tid, phase_num, req.message, now))
        db.execute("INSERT INTO messages (trial_id, phase_num, role, content, ts) VALUES (%s,%s,'assistant',%s,%s)",
                   (tid, phase_num, reply, now + 0.001))

    return {"reply": reply}

# ── Astra messages ─────────────────────────────────────────────────────────────
@app.get("/api/astra")
def get_astra():
    trial = _get_trial()
    with _db() as db:
        db.execute(
            "SELECT phase_num, content, ts FROM messages WHERE trial_id=%s AND role='astra' ORDER BY ts",
            (trial["id"],)
        )
        rows = db.fetchall()
    return [{"phase": r["phase_num"], "content": r["content"], "ts": r["ts"]} for r in rows]

# ── Config API ─────────────────────────────────────────────────────────────────
@app.get("/api/config")
def get_config(key: str = ""):
    if key.upper() != _admin_key().upper():
        raise HTTPException(403, "Unauthorized")
    k = _api_key()
    return {
        "api_key_set":     bool(k),
        "api_key_preview": (k[:8] + "..." + k[-4:]) if len(k) > 14 else ("(set)" if k else ""),
        "access_code":     _access_code(),
        "admin_key":       _admin_key(),
        "candidate_name":  _candidate_name(),
    }

class ConfigUpdateReq(BaseModel):
    anthropic_api_key: Optional[str] = None
    access_code:       Optional[str] = None
    candidate_name:    Optional[str] = None

@app.post("/api/config")
def update_config(req: ConfigUpdateReq, key: str = ""):
    if key.upper() != _admin_key().upper():
        raise HTTPException(403, "Unauthorized")
    if req.anthropic_api_key is not None:
        _set_cfg("anthropic_api_key", req.anthropic_api_key.strip())
    if req.access_code and req.access_code.strip():
        new_code = req.access_code.strip().upper()
        _set_cfg("access_code", new_code)
        with _db() as db:
            db.execute("UPDATE trial SET access_code = %s WHERE id = 1", (new_code,))
    if req.candidate_name and req.candidate_name.strip():
        _set_cfg("candidate_name", req.candidate_name.strip())
        with _db() as db:
            db.execute("UPDATE trial SET name = %s WHERE id = 1", (req.candidate_name.strip(),))
    return {"ok": True, "api_key_set": bool(_api_key())}

# ── Admin ──────────────────────────────────────────────────────────────────────
@app.get("/api/admin")
def admin_view(key: str = ""):
    if key.upper() != _admin_key().upper():
        raise HTTPException(403, "Unauthorized")
    trial = _get_trial()
    tid   = trial["id"]

    with _db() as db:
        db.execute("SELECT * FROM phases WHERE trial_id=%s ORDER BY phase_num", (tid,))
        phases = db.fetchall()
        db.execute("SELECT * FROM messages WHERE trial_id=%s ORDER BY ts", (tid,))
        msgs = db.fetchall()
        db.execute("SELECT * FROM report WHERE trial_id=%s", (tid,))
        rep = db.fetchone()

    phases_out = []
    for ph in phases:
        ev = json.loads(ph["eval_raw"]) if ph["eval_raw"] else {}
        phases_out.append({
            "phase_num":    ph["phase_num"],
            "status":       ph["status"],
            "score":        ph["score"],
            "eval":         ev,
            "submission":   ph["submission_text"],
            "submitted_at": ph["submitted_at"],
        })

    return {
        "candidate":  trial["name"],
        "status":     trial["status"],
        "start_time": trial["start_time"],
        "time_left":  _fmt_time(_time_left(trial["start_time"])) if trial["start_time"] else None,
        "phases":     phases_out,
        "messages":   [{"phase": m["phase_num"], "role": m["role"],
                        "content": m["content"], "ts": m["ts"]} for m in msgs],
        "report":     dict(rep) if rep else None,
    }

@app.post("/api/admin/override")
def admin_override(phase_num: int, passed: bool, key: str = ""):
    if key.upper() != _admin_key().upper():
        raise HTTPException(403, "Unauthorized")
    trial = _get_trial()
    tid   = trial["id"]
    with _db() as db:
        db.execute("UPDATE phases SET status=%s, score=%s WHERE trial_id=%s AND phase_num=%s",
                   ("passed" if passed else "failed", 100 if passed else 0, tid, phase_num))
        if passed and phase_num < 4:
            next_pn  = phase_num + 1
            deadline = (time.time() + PHASE4_SUB_HOURS * 3600) if next_pn == 4 else None
            db.execute(
                "UPDATE phases SET status='active', unlocked_at=%s, sub_deadline=%s WHERE trial_id=%s AND phase_num=%s",
                (time.time(), deadline, tid, next_pn)
            )
    if passed and phase_num == 4:
        _finalize_trial(tid)
    return {"ok": True}

# Vercel handler export
from mangum import Mangum
handler = Mangum(app, lifespan="off")
