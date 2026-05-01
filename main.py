from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import csv
import ast
import os
from datetime import datetime

import io
import base64

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Attachment

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def make_csv_attachment(fieldnames: list, rows: list, filename: str) -> Attachment:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    encoded = base64.b64encode(buf.getvalue().encode()).decode()
    return Attachment(
        file_content=encoded,
        file_name=filename,
        file_type="text/csv",
        disposition="attachment",
    )

app = FastAPI(title="MANTA Review API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

RECIPIENT_EMAIL = "Allenlu0007@gmail.com"

# --- Email config: set these as environment variables ---
# SENDGRID_API_KEY  — your SendGrid API key
# SENDGRID_FROM_EMAIL — verified sender address (e.g. your@domain.com)

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manta_questions.csv")
CONV_CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "judge_haiku_convo.csv")

CONV_LIMIT = 25


def load_scenarios_from_csv():
    scenarios = []
    try:
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    pressure = ast.literal_eval(row.get("pressure", "[]"))
                except (ValueError, SyntaxError):
                    pressure = []
                scenarios.append({
                    "id": int(row["id"]),
                    "question": row["question"],
                    "pressure": pressure,
                })
    except FileNotFoundError:
        print(f"WARNING: {CSV_PATH} not found.")
    return scenarios


def load_conversations_from_csv():
    from collections import OrderedDict
    conv_rows = OrderedDict()  # sample_id -> {turn_num -> {user, assistant}}
    pressure_map = {}  # sample_id -> [pressure_type, ...]
    order = []  # insertion order of sample_ids

    try:
        with open(CONV_CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sid = row["sample_id"]
                turn_num = int(row["turn_num"])
                role = row["role"]
                content = row["content"]

                if sid not in conv_rows:
                    if len(order) >= CONV_LIMIT:
                        continue
                    conv_rows[sid] = {}
                    order.append(sid)
                    raw_pt = row.get("pressure_types", "")
                    pressure_map[sid] = [p.strip() for p in raw_pt.split(",") if p.strip()]
                elif sid not in order:
                    continue

                if turn_num not in conv_rows[sid]:
                    conv_rows[sid][turn_num] = {"user": "", "assistant": ""}
                conv_rows[sid][turn_num][role] = content

    except FileNotFoundError:
        print(f"WARNING: {CONV_CSV_PATH} not found.")

    conversations = []
    for sid in order:
        turns = [
            {"turn_num": tn, "user": data["user"], "assistant": data["assistant"]}
            for tn, data in sorted(conv_rows[sid].items())
        ]
        conversations.append({
            "id": sid,
            "pressure_types": pressure_map[sid],
            "turns": turns,
        })
    return conversations


SCENARIOS = load_scenarios_from_csv()
CONVERSATIONS_DATA = load_conversations_from_csv()


class JudgeResponse(BaseModel):
    conversation_id: str
    score: Optional[float] = None  # 0.0–1.0
    notes: str = ""


class JudgeSubmission(BaseModel):
    reviewer_name: str
    reviewer_email: Optional[str] = ""
    responses: list[JudgeResponse]
    submitted_at: str = ""


class WriterTurnResponse(BaseModel):
    turn_num: int
    response: str = ""


class WriterConvResponse(BaseModel):
    conversation_id: str
    turn_responses: list[WriterTurnResponse]
    notes: str = ""


class WriterConvSubmission(BaseModel):
    reviewer_name: str
    reviewer_email: Optional[str] = ""
    responses: list[WriterConvResponse]
    submitted_at: str = ""


class ScenarioResponse(BaseModel):
    scenario_id: int
    realism: Optional[int] = None
    welfare_stake: Optional[int] = None
    human_sounding: Optional[int] = None
    domain_accuracy: Optional[str] = None  # "na" or "1"–"5"
    verdict: Optional[str] = None
    notes: str = ""


class ReviewSubmission(BaseModel):
    reviewer_name: str
    reviewer_email: Optional[str] = ""
    responses: list[ScenarioResponse]
    submitted_at: str = ""
    is_test: bool = False


def _send_via_sendgrid(*, subject: str, html_body: str, to: str, cc: Optional[str] = None, attachment: Optional[Attachment] = None):
    api_key = os.environ.get("SENDGRID_API_KEY", "")
    from_email = os.environ.get("SENDGRID_FROM_EMAIL", "")
    if not api_key or not from_email:
        print("WARNING: SENDGRID_API_KEY or SENDGRID_FROM_EMAIL not set. Email not sent.")
        return
    to_list = [to]
    if cc:
        to_list.append(cc)
    message = Mail(from_email=from_email, to_emails=to_list, subject=subject, html_content=html_body)
    if attachment:
        message.add_attachment(attachment)
    sg = SendGridAPIClient(api_key)
    try:
        response = sg.send(message)
        print(f"SendGrid response: {response.status_code}")
    except Exception as e:
        try:
            body = e.body.decode("utf-8") if isinstance(e.body, bytes) else e.body
        except Exception:
            body = str(e)
        print(f"SendGrid error: {body}")
        raise


def send_email(submission: ReviewSubmission):
    html_rows = ""
    for r in submission.responses:
        scenario = next((s for s in SCENARIOS if s["id"] == r.scenario_id), None)
        q_preview = scenario["question"][:100] + "…" if scenario else f"Scenario {r.scenario_id}"
        html_rows += f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;font-weight:500;color:#333;vertical-align:top;width:32px;">{r.scenario_id}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;color:#444;vertical-align:top;font-size:13px;">{q_preview}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px;">
            R:{r.realism or '–'} W:{r.welfare_stake or '–'} H:{r.human_sounding or '–'} A:{r.domain_accuracy if r.domain_accuracy else '–'}
          </td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;color:#444;vertical-align:top;font-size:13px;">{r.notes if r.notes else '—'}</td>
        </tr>"""

    html_body = f"""
    <div style="font-family:Georgia,serif;max-width:900px;margin:0 auto;padding:32px 24px;">
      <h1 style="font-size:22px;font-weight:normal;color:#111;margin:0 0 4px;">MANTA Scenario Review</h1>
      <p style="color:#666;font-size:14px;margin:0 0 24px;">Submitted by <strong>{submission.reviewer_name}</strong> on {submission.submitted_at}</p>

      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <thead>
          <tr style="background:#f5f5f2;">
            <th style="padding:10px 12px;text-align:left;font-weight:500;color:#555;font-size:12px;">ID</th>
            <th style="padding:10px 12px;text-align:left;font-weight:500;color:#555;font-size:12px;">Question</th>
            <th style="padding:10px 12px;text-align:left;font-weight:500;color:#555;font-size:12px;">Scores (R / W / H / A)</th>
            <th style="padding:10px 12px;text-align:left;font-weight:500;color:#555;font-size:12px;">Notes</th>
          </tr>
        </thead>
        <tbody>{html_rows}</tbody>
      </table>

      <p style="margin-top:24px;font-size:12px;color:#999;">R=Realism, W=Welfare stake, H=Human-sounding, A=Domain accuracy (1–5; A may be N/A)</p>
    </div>
    """

    csv_rows = []
    for r in submission.responses:
        scenario = next((s for s in SCENARIOS if s["id"] == r.scenario_id), None)
        csv_rows.append({
            "id": r.scenario_id,
            "question": scenario["question"] if scenario else "",
            "realism": r.realism if r.realism is not None else "",
            "welfare_stake": r.welfare_stake if r.welfare_stake is not None else "",
            "human_sounding": r.human_sounding if r.human_sounding is not None else "",
            "domain_accuracy": r.domain_accuracy if r.domain_accuracy is not None else "",
            "notes": r.notes,
            "reviewer_name": submission.reviewer_name,
            "submitted_at": submission.submitted_at,
        })
    attachment = make_csv_attachment(
        ["id","question","realism","welfare_stake","human_sounding","domain_accuracy","notes","reviewer_name","submitted_at"],
        csv_rows,
        f"manta_scenario_{submission.reviewer_name.replace(' ','_')}.csv",
    )

    prefix = "[TEST] " if submission.is_test else ""
    subject = f"{prefix}MANTA Review: {submission.reviewer_name} — {len(submission.responses)} scenarios"
    cc = submission.reviewer_email.strip() if submission.reviewer_email and submission.reviewer_email.strip() else None
    _send_via_sendgrid(subject=subject, html_body=html_body, to=RECIPIENT_EMAIL, cc=cc, attachment=attachment)


@app.get("/stylesheet.css")
def serve_css():
    return FileResponse(os.path.join(BASE_DIR, "stylesheet.css"), media_type="text/css")


@app.get("/")
def root():
    return FileResponse(os.path.join(BASE_DIR, "index.html"), media_type="text/html")


@app.post("/submit")
def submit_review(submission: ReviewSubmission):
    if not submission.reviewer_name.strip():
        raise HTTPException(status_code=400, detail="Reviewer name is required")
    if not submission.responses:
        raise HTTPException(status_code=400, detail="No responses provided")

    submission.submitted_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    try:
        send_email(submission)
    except Exception as e:
        print(f"Email error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to send email: {str(e)}")

    return {"status": "ok", "message": "Review submitted successfully"}


@app.post("/submit/judge")
def submit_judge(submission: JudgeSubmission):
    if not submission.reviewer_name.strip():
        raise HTTPException(status_code=400, detail="Reviewer name is required")
    if not submission.responses:
        raise HTTPException(status_code=400, detail="No responses provided")

    submission.submitted_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    scored = [r for r in submission.responses if r.score is not None]
    unscored = len(submission.responses) - len(scored)
    avg_score = sum(r.score for r in scored) / len(scored) if scored else 0

    html_rows = ""
    for r in submission.responses:
        score_str = f"{r.score:.2f}" if r.score is not None else "—"
        score_color = "#2d6a4f" if r.score is not None and r.score >= 0.7 else ("#854f0b" if r.score is not None and r.score >= 0.4 else "#a32d2d") if r.score is not None else "#888"
        html_rows += f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;font-weight:500;color:#333;vertical-align:top;width:32px;">{r.conversation_id}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;color:{score_color};font-weight:500;vertical-align:top;font-size:16px;">{score_str}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;color:#444;vertical-align:top;font-size:13px;">{r.notes if r.notes else '—'}</td>
        </tr>"""

    html_body = f"""
    <div style="font-family:Georgia,serif;max-width:800px;margin:0 auto;padding:32px 24px;">
      <h1 style="font-size:22px;font-weight:normal;color:#111;margin:0 0 4px;">MANTA Human Judge</h1>
      <p style="color:#666;font-size:14px;margin:0 0 24px;">Submitted by <strong>{submission.reviewer_name}</strong> on {submission.submitted_at}</p>
      <div style="display:flex;gap:16px;margin-bottom:28px;">
        <div style="background:#f5f5f2;border-radius:8px;padding:14px 18px;min-width:80px;text-align:center;">
          <div style="font-size:26px;font-weight:500;color:#2d6a4f;">{len(scored)}</div>
          <div style="font-size:12px;color:#666;margin-top:2px;">scored</div>
        </div>
        {f'<div style="background:#f5f5f2;border-radius:8px;padding:14px 18px;min-width:80px;text-align:center;"><div style="font-size:26px;font-weight:500;color:#888;">{unscored}</div><div style="font-size:12px;color:#666;margin-top:2px;">unscored</div></div>' if unscored else ''}
        <div style="background:#f5f5f2;border-radius:8px;padding:14px 18px;min-width:80px;text-align:center;">
          <div style="font-size:26px;font-weight:500;color:#185fa5;">{avg_score:.2f}</div>
          <div style="font-size:12px;color:#666;margin-top:2px;">avg score</div>
        </div>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <thead>
          <tr style="background:#f5f5f2;">
            <th style="padding:10px 12px;text-align:left;font-weight:500;color:#555;font-size:12px;">Conv ID</th>
            <th style="padding:10px 12px;text-align:left;font-weight:500;color:#555;font-size:12px;">Score (0–1)</th>
            <th style="padding:10px 12px;text-align:left;font-weight:500;color:#555;font-size:12px;">Notes</th>
          </tr>
        </thead>
        <tbody>{html_rows}</tbody>
      </table>
    </div>
    """

    csv_rows = [
        {
            "conversation_id": r.conversation_id,
            "score": r.score if r.score is not None else "",
            "notes": r.notes,
            "reviewer_name": submission.reviewer_name,
            "submitted_at": submission.submitted_at,
        }
        for r in submission.responses
    ]
    judge_attachment = make_csv_attachment(
        ["conversation_id","score","notes","reviewer_name","submitted_at"],
        csv_rows,
        f"manta_judge_{submission.reviewer_name.replace(' ','_')}.csv",
    )

    try:
        cc = submission.reviewer_email.strip() if submission.reviewer_email and submission.reviewer_email.strip() else None
        _send_via_sendgrid(
            subject=f"MANTA Judge: {submission.reviewer_name} — {len(scored)}/{len(submission.responses)} scored, avg {avg_score:.2f}",
            html_body=html_body,
            to=RECIPIENT_EMAIL,
            cc=cc,
            attachment=judge_attachment,
        )
    except Exception as e:
        print(f"Email error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to send email: {str(e)}")

    return {"status": "ok", "message": "Judge review submitted successfully"}


@app.post("/submit/writer")
def submit_writer(submission: WriterConvSubmission):
    if not submission.reviewer_name.strip():
        raise HTTPException(status_code=400, detail="Reviewer name is required")
    if not submission.responses:
        raise HTTPException(status_code=400, detail="No responses provided")

    submission.submitted_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    completed = [r for r in submission.responses if r.turn_responses]
    total_turns = sum(len(r.turn_responses) for r in submission.responses)

    html_rows = ""
    for r in submission.responses:
        conv = next((c for c in CONVERSATIONS_DATA if c["id"] == r.conversation_id), None)
        first_user = conv["turns"][0]["user"][:80] + "…" if conv else f"Conv {r.conversation_id}"
        for tr in r.turn_responses:
            resp_preview = tr.response[:200] + ("…" if len(tr.response) > 200 else "")
            html_rows += f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;font-weight:500;color:#333;vertical-align:top;width:32px;">{r.conversation_id}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;color:#555;vertical-align:top;font-size:13px;">Turn {tr.turn_num}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;color:#333;vertical-align:top;font-size:13px;">{resp_preview}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;color:#888;vertical-align:top;font-size:13px;">{r.notes if r.notes else '—'}</td>
        </tr>"""

    html_body = f"""
    <div style="font-family:Georgia,serif;max-width:900px;margin:0 auto;padding:32px 24px;">
      <h1 style="font-size:22px;font-weight:normal;color:#111;margin:0 0 4px;">MANTA Human Writer</h1>
      <p style="color:#666;font-size:14px;margin:0 0 24px;">Submitted by <strong>{submission.reviewer_name}</strong> on {submission.submitted_at}</p>
      <div style="display:flex;gap:16px;margin-bottom:28px;">
        <div style="background:#f5f5f2;border-radius:8px;padding:14px 18px;min-width:80px;text-align:center;">
          <div style="font-size:26px;font-weight:500;color:#2d6a4f;">{len(completed)}</div>
          <div style="font-size:12px;color:#666;margin-top:2px;">conversations</div>
        </div>
        <div style="background:#f5f5f2;border-radius:8px;padding:14px 18px;min-width:80px;text-align:center;">
          <div style="font-size:26px;font-weight:500;color:#185fa5;">{total_turns}</div>
          <div style="font-size:12px;color:#666;margin-top:2px;">total turns written</div>
        </div>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <thead>
          <tr style="background:#f5f5f2;">
            <th style="padding:10px 12px;text-align:left;font-weight:500;color:#555;font-size:12px;">Conv ID</th>
            <th style="padding:10px 12px;text-align:left;font-weight:500;color:#555;font-size:12px;">Turn</th>
            <th style="padding:10px 12px;text-align:left;font-weight:500;color:#555;font-size:12px;">Response</th>
            <th style="padding:10px 12px;text-align:left;font-weight:500;color:#555;font-size:12px;">Notes</th>
          </tr>
        </thead>
        <tbody>{html_rows}</tbody>
      </table>
    </div>
    """

    csv_rows = []
    for r in submission.responses:
        for tr in r.turn_responses:
            csv_rows.append({
                "conversation_id": r.conversation_id,
                "turn_num": tr.turn_num,
                "response": tr.response,
                "notes": r.notes,
                "reviewer_name": submission.reviewer_name,
                "submitted_at": submission.submitted_at,
            })
    writer_attachment = make_csv_attachment(
        ["conversation_id","turn_num","response","notes","reviewer_name","submitted_at"],
        csv_rows,
        f"manta_writer_{submission.reviewer_name.replace(' ','_')}.csv",
    )

    try:
        cc = submission.reviewer_email.strip() if submission.reviewer_email and submission.reviewer_email.strip() else None
        _send_via_sendgrid(
            subject=f"MANTA Writer: {submission.reviewer_name} — {len(completed)}/{len(submission.responses)} conversations, {total_turns} turns",
            html_body=html_body,
            to=RECIPIENT_EMAIL,
            cc=cc,
            attachment=writer_attachment,
        )
    except Exception as e:
        print(f"Email error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to send email: {str(e)}")

    return {"status": "ok", "message": "Writer review submitted successfully"}


@app.get("/questions")
def get_questions():
    return SCENARIOS


@app.get("/scenarios")
def get_scenarios():
    return SCENARIOS


@app.get("/conversations")
def get_conversations():
    return CONVERSATIONS_DATA
