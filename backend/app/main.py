from contextlib import asynccontextmanager
import asyncio
from datetime import date, datetime, timezone
import logging
import re
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .config import settings
from .database import Base, engine, get_db
from .database import SessionLocal
from . import models
from .agent.actions import execute_approval_action, execute_explicit_chat_action, follows_calendar_approval_prompt, is_confirmation_only
from .integrations.google_gmail import (
    CALLBACK_URI,
    build_gmail_service,
    cancel_authorization,
    calendar_connected,
    create_authorization_url,
    credentials_file,
    exchange_authorization_code,
    gmail_connected,
    list_calendar_events,
)
from .integrations.telegram_bot import configured_bot
from .services.telegram_service import telegram_polling_loop
from .services.reminder_service import reminder_worker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jarvis")


def _reminder_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    bot_enabled = configured_bot() is not None
    polling_task = asyncio.create_task(telegram_polling_loop()) if bot_enabled else None
    reminder_task = asyncio.create_task(reminder_worker()) if bot_enabled else None
    try:
        yield
    finally:
        if polling_task:
            polling_task.cancel()
            await asyncio.gather(polling_task, return_exceptions=True)
        if reminder_task:
            reminder_task.cancel()
            await asyncio.gather(reminder_task, return_exceptions=True)


app = FastAPI(title="JARVIS Personal Assistant", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[settings.frontend_origin, "http://127.0.0.1:5173"], allow_methods=["*"], allow_headers=["*"])


def authorize(x_jarvis_key: Optional[str] = Header(default=None)):
    if settings.dashboard_api_key and x_jarvis_key != settings.dashboard_api_key:
        raise HTTPException(401, "Dashboard API key is missing or invalid")


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    description: str = ""
    priority: str = "medium"
    category: str = "Personal"
    due_date: str | None = None
    estimated_minutes: int = Field(default=30, ge=1, le=1440)


class TaskPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=240)
    description: str | None = Field(default=None, max_length=4000)
    priority: str | None = None
    category: str | None = Field(default=None, max_length=80)
    due_date: str | None = Field(default=None, max_length=40)
    estimated_minutes: int | None = Field(default=None, ge=1, le=1440)
    completed: bool | None = None


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    conversation_id: int | None = None


class MemoryIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    category: str = "Preference"


class ApprovalIn(BaseModel):
    action: str = Field(min_length=1, max_length=120)
    target: str = ""
    details: str = ""


@app.get("/api/health")
def health():
    return {"status": "ok", "name": settings.app_name, "ai_configured": bool(settings.gemini_api_key), "timezone": settings.timezone}


@app.get("/api/dashboard", dependencies=[Depends(authorize)])
def dashboard(db: Session = Depends(get_db)):
    tasks = db.scalars(select(models.Task).order_by(models.Task.completed, models.Task.due_date)).all()
    reminders = db.scalars(select(models.TaskReminder).where(models.TaskReminder.sent_at.is_(None))).all()
    reminder_by_task = {reminder.task_id: _reminder_utc(reminder.scheduled_at) for reminder in reminders}
    task_payload = [{**{column.key: getattr(task, column.key) for column in models.Task.__table__.columns}, "reminder_at": reminder_by_task.get(task.id)} for task in tasks]
    pending = db.scalar(select(func.count()).select_from(models.Approval).where(models.Approval.status == "pending")) or 0
    done = sum(t.completed for t in tasks)
    connected = gmail_connected()
    return {"tasks": task_payload, "stats": {"total_tasks": len(tasks), "completed_tasks": done, "open_tasks": len(tasks)-done, "pending_approvals": pending}, "integrations": {"gemini": bool(settings.gemini_api_key), "telegram": bool(settings.telegram_bot_token and settings.telegram_user_id), "google": connected, "gmail": connected}}


@app.get("/api/tasks", dependencies=[Depends(authorize)])
def list_tasks(db: Session = Depends(get_db)):
    tasks = db.scalars(select(models.Task).order_by(models.Task.completed, models.Task.due_date, models.Task.created_at.desc())).all()
    reminders = db.scalars(select(models.TaskReminder).where(models.TaskReminder.sent_at.is_(None))).all()
    by_task = {reminder.task_id: _reminder_utc(reminder.scheduled_at) for reminder in reminders}
    return [{**{column.key: getattr(task, column.key) for column in models.Task.__table__.columns}, "reminder_at": by_task.get(task.id)} for task in tasks]


@app.post("/api/tasks", dependencies=[Depends(authorize)])
def create_task(data: TaskIn, db: Session = Depends(get_db)):
    if data.priority not in {"low", "medium", "high", "urgent"}:
        raise HTTPException(422, "Priority must be low, medium, high, or urgent")
    task = models.Task(**data.model_dump())
    db.add(task); db.commit(); db.refresh(task)
    return task


@app.patch("/api/tasks/{task_id}", dependencies=[Depends(authorize)])
def update_task(task_id: int, data: TaskPatch, db: Session = Depends(get_db)):
    task = db.get(models.Task, task_id)
    if not task: raise HTTPException(404, "Task not found")
    values = data.model_dump(exclude_unset=True)
    if values.get("priority") is not None and values["priority"] not in {"low", "medium", "high", "urgent"}:
        raise HTTPException(422, "Priority must be low, medium, high, or urgent")
    for key, value in values.items():
        if value is None and key not in {"due_date"}:
            raise HTTPException(422, f"{key} cannot be empty")
        setattr(task, key, value)
    if values.get("completed") is True:
        task.completed_at = datetime.now(timezone.utc)
        db.query(models.TaskReminder).filter(models.TaskReminder.task_id == task_id).delete(synchronize_session=False)
    if values.get("completed") is False: task.completed_at = None
    db.commit(); db.refresh(task); return task


@app.delete("/api/tasks/{task_id}", dependencies=[Depends(authorize)])
def delete_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(models.Task, task_id)
    if not task: raise HTTPException(404, "Task not found")
    db.query(models.TaskReminder).filter(models.TaskReminder.task_id == task_id).delete(synchronize_session=False)
    db.delete(task); db.commit(); return {"ok": True}


@app.get("/api/memories", dependencies=[Depends(authorize)])
def list_memories(db: Session = Depends(get_db)):
    return db.scalars(select(models.Memory).order_by(models.Memory.created_at.desc())).all()


@app.post("/api/memories", dependencies=[Depends(authorize)])
def create_memory(data: MemoryIn, db: Session = Depends(get_db)):
    memory = models.Memory(**data.model_dump()); db.add(memory); db.commit(); db.refresh(memory); return memory


@app.patch("/api/memories/{memory_id}", dependencies=[Depends(authorize)])
def update_memory(memory_id: int, data: MemoryIn, db: Session = Depends(get_db)):
    memory = db.get(models.Memory, memory_id)
    if not memory: raise HTTPException(404, "Memory not found")
    memory.content = data.content; memory.category = data.category; db.commit(); db.refresh(memory); return memory


@app.delete("/api/memories/{memory_id}", dependencies=[Depends(authorize)])
def delete_memory(memory_id: int, db: Session = Depends(get_db)):
    memory = db.get(models.Memory, memory_id)
    if not memory: raise HTTPException(404, "Memory not found")
    db.delete(memory); db.commit(); return {"ok": True}


@app.get("/api/approvals", dependencies=[Depends(authorize)])
def list_approvals(db: Session = Depends(get_db)):
    return db.scalars(select(models.Approval).order_by(models.Approval.created_at.desc())).all()


@app.post("/api/approvals", dependencies=[Depends(authorize)])
def create_approval(data: ApprovalIn, db: Session = Depends(get_db)):
    approval = models.Approval(**data.model_dump()); db.add(approval); db.commit(); db.refresh(approval); return approval


@app.post("/api/approvals/{approval_id}/{decision}", dependencies=[Depends(authorize)])
def decide_approval(approval_id: int, decision: str, db: Session = Depends(get_db)):
    if decision not in {"approve", "reject"}: raise HTTPException(422, "Decision must be approve or reject")
    approval = db.get(models.Approval, approval_id)
    if not approval: raise HTTPException(404, "Approval not found")
    if approval.status != "pending": raise HTTPException(409, "Approval has already been decided")
    action_result = "Approval rejected; no external action was taken."
    if decision == "approve":
        action_result = execute_approval_action(approval)
    approval.status = decision; approval.decided_at = datetime.now(timezone.utc)
    db.add(models.ActionLog(action=approval.action, result=action_result))
    db.commit(); db.refresh(approval); return approval


@app.get("/api/conversations", dependencies=[Depends(authorize)])
def list_conversations(db: Session = Depends(get_db)):
    return db.scalars(select(models.Conversation).order_by(models.Conversation.created_at.desc())).all()


@app.get("/api/conversations/{conversation_id}/messages", dependencies=[Depends(authorize)])
def list_messages(conversation_id: int, db: Session = Depends(get_db)):
    return db.scalars(select(models.Message).where(models.Message.conversation_id == conversation_id).order_by(models.Message.created_at)).all()


@app.delete("/api/conversations/{conversation_id}", dependencies=[Depends(authorize)])
def delete_conversation(conversation_id: int, db: Session = Depends(get_db)):
    conversation = db.get(models.Conversation, conversation_id)
    if not conversation:
        raise HTTPException(404, "Conversation not found")
    db.query(models.Message).filter(models.Message.conversation_id == conversation_id).delete(synchronize_session=False)
    db.delete(conversation)
    db.commit()
    return {"ok": True}


@app.post("/api/chat", dependencies=[Depends(authorize)])
async def chat(data: ChatIn, db: Session = Depends(get_db)):
    conversation = db.get(models.Conversation, data.conversation_id) if data.conversation_id else None
    if not conversation:
        conversation = models.Conversation(title=data.message[:70]); db.add(conversation); db.commit(); db.refresh(conversation)
    history = db.scalars(select(models.Message).where(models.Message.conversation_id == conversation.id).order_by(models.Message.created_at.desc()).limit(12)).all()
    db.add(models.Message(conversation_id=conversation.id, role="user", content=data.message)); db.commit()
    if re.search(r"\b(?:what(?:'s| is)?\s+the\s+time|time\s+(?:is\s+it|right\s+now)|current\s+time|tell\s+me\s+the\s+time)\b", data.message, re.IGNORECASE):
        now_local = datetime.now(ZoneInfo(settings.timezone))
        answer = f"It is {now_local.strftime('%I:%M %p').lstrip('0')} in {settings.timezone}, Sir."
    elif is_confirmation_only(data.message) and follows_calendar_approval_prompt(history):
        answer = "I haven't added anything from that reply. Calendar events appear in Approvals first, and only the Approve button there can add them. If you don't see the event proposal, ask me to prepare it again."
    elif is_confirmation_only(data.message):
        # Short acknowledgements do not need a round trip to Gemini.
        answer = "Got it. What would you like me to do next?"
    else:
        answer = await execute_explicit_chat_action(data.message, db)
    if answer is None:
        if not settings.gemini_api_key:
            answer = "Gemini is not configured yet. Add GEMINI_API_KEY to backend/.env to enable AI chat. Your message has been saved."
        else:
            answer = None
    if answer is None:
        try:
            from google import genai
            from google.genai import types
            client = genai.Client(
                api_key=settings.gemini_api_key,
                http_options=types.HttpOptions(
                    timeout=30000,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
            turns = [f"{m.role}: {m.content}" for m in reversed(history)]
            turns.append(f"user: {data.message}")
            memories = db.scalars(select(models.Memory).limit(20)).all()
            prompt = ("You are JARVIS, the user's highly professional, capable personal assistant. Address the user respectfully as Sir in a natural way, without repeating it in every sentence. Be polished, concise, calm, proactive, and courteous. "
                      "Be honest about uncertainty and never claim an action succeeded unless the app confirms it. In this app, direct task requests can create a task; requested email drafts can be saved to Gmail when draft permission is connected and are never sent; Google Calendar events are proposed and created only after explicit approval. "
                      "You cannot control the computer's or phone's native calendar/alarm app. You can schedule a task reminder to the user's private Telegram when Telegram is configured; a reminder only notifies the user and never sends an email. Never claim to set a reminder unless the task action confirms it. "
                      "Never treat ordinary conversation as approval for an external action. ")
            if memories: prompt += "Known user preferences: " + "; ".join(m.content for m in memories) + "\n"
            try:
                response = await run_in_threadpool(
                    client.models.generate_content,
                    model=settings.gemini_model,
                    contents=prompt + "\n".join(turns),
                    config=types.GenerateContentConfig(
                        thinking_config=types.ThinkingConfig(thinking_level="minimal")
                    ),
                )
            finally:
                client.close()
            answer = response.text or "I couldn't produce a response. Please try again."
        except Exception as exc:
            code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            # SDK/network exception text is useful for troubleshooting, but never let
            # a credential-like token escape into logs or the chat response.
            detail = str(getattr(exc, "message", None) or exc).strip()
            detail = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[redacted API key]", detail)
            detail = detail[:240]
            logger.warning("Gemini request failed (%s, status=%s): %s", type(exc).__name__, code, detail)
            if code in (401, 403):
                answer = "Gemini rejected the API key or this project doesn't have API access. Check GEMINI_API_KEY in backend/.env and confirm the Gemini API is enabled for that key. Restart the backend after changing .env."
            elif code == 429:
                answer = "Gemini's quota or rate limit was reached. Check the quota and billing status for this API key, then try again later."
            elif code == 400:
                answer = f"Gemini rejected the request (HTTP 400). Check that GEMINI_MODEL is available to your API key; it is currently set to {settings.gemini_model}."
            elif isinstance(code, int) and code >= 500:
                answer = f"Gemini is having a temporary service problem (HTTP {code}). Please try again shortly."
            elif "timeout" in type(exc).__name__.lower() or "timed out" in detail.lower():
                answer = "Gemini took too long to respond. Check your internet or proxy connection and try again. The chat request timed out safely."
            else:
                reason = f" Details: {detail}" if detail else ""
                answer = f"JARVIS couldn't connect to Gemini ({type(exc).__name__}). Check that the backend has internet access, then retry. If you just added or changed the API key, restart the backend first.{reason}"
    db.add(models.Message(conversation_id=conversation.id, role="assistant", content=answer)); db.commit()
    return {"conversation_id": conversation.id, "answer": answer}


@app.get("/api/settings", dependencies=[Depends(authorize)])
def get_settings():
    connected = gmail_connected()
    from .integrations.google_gmail import calendar_write_connected, gmail_drafts_connected
    return {"timezone": settings.timezone, "gemini_configured": bool(settings.gemini_api_key), "telegram_configured": bool(settings.telegram_bot_token and settings.telegram_user_id), "google_configured": connected, "gmail_configured": connected, "gmail_drafts_configured": gmail_drafts_connected(), "calendar_configured": calendar_connected(), "calendar_event_creation_configured": calendar_write_connected(), "web_search_configured": False}


@app.get("/api/telegram/status", dependencies=[Depends(authorize)])
def telegram_status():
    return {"configured": configured_bot() is not None, "polling": configured_bot() is not None}


@app.get("/api/gmail/status", dependencies=[Depends(authorize)])
def gmail_status():
    from .integrations.google_gmail import gmail_drafts_connected
    return {"connected": gmail_connected(), "drafts_enabled": gmail_drafts_connected(), "oauth_client_configured": credentials_file().exists(), "scope": "gmail.readonly", "draft_scope": "gmail.compose"}


@app.get("/api/gmail/oauth/start", dependencies=[Depends(authorize)])
def gmail_oauth_start():
    return {"authorization_url": create_authorization_url("gmail"), "redirect_uri": CALLBACK_URI}


@app.get("/api/calendar/status", dependencies=[Depends(authorize)])
def calendar_status():
    from .integrations.google_gmail import calendar_write_connected
    return {"connected": calendar_connected(), "event_creation_enabled": calendar_write_connected(), "oauth_client_configured": credentials_file().exists(), "scope": "calendar.readonly", "write_scope": "calendar.events.owned"}


@app.get("/api/calendar/oauth/start", dependencies=[Depends(authorize)])
def calendar_oauth_start():
    return {"authorization_url": create_authorization_url("calendar"), "redirect_uri": CALLBACK_URI}


@app.get("/api/calendar/events", dependencies=[Depends(authorize)])
def calendar_events(day: date | None = Query(default=None)):
    target_day = day or datetime.now(ZoneInfo(settings.timezone)).date()
    return list_calendar_events(target_day)


@app.get("/api/gmail/oauth/callback")
def gmail_oauth_callback(state: str, code: str | None = None, error: str | None = None):
    if error:
        destination = cancel_authorization(state)
        return RedirectResponse(url=f"{settings.frontend_origin}/?{destination}=denied")
    if not code:
        raise HTTPException(400, "Google did not return an authorization code. Start the connection again.")
    destination = exchange_authorization_code(state, code)
    return RedirectResponse(url=f"{settings.frontend_origin}/?{destination}=connected")


def _email_category(subject: str, preview: str) -> str:
    text = f"{subject} {preview}".lower()
    if any(word in text for word in ("urgent", "immediately", "action required", "asap", "overdue")):
        return "Urgent"
    if any(word in text for word in ("invoice", "payment", "bank", "statement", "tax", "fee", "transaction")):
        return "Financial"
    if any(word in text for word in ("internship", "interview", "career", "job offer", "recruiter", "application")):
        return "Career"
    if any(word in text for word in ("assignment", "lecture", "college", "university", "course", "exam", "professor")):
        return "College"
    if any(word in text for word in ("unsubscribe", "sale", "discount", "offer", "newsletter", "promotion")):
        return "Promotions"
    if any(word in text for word in ("important", "please review", "deadline", "required", "please respond")):
        return "Important"
    return "Other"


@app.get("/api/gmail/messages", dependencies=[Depends(authorize)])
def gmail_messages(limit: int = Query(default=15, ge=1, le=50), db: Session = Depends(get_db)):
    service = build_gmail_service()
    try:
        result = service.users().messages().list(userId="me", q="is:unread", maxResults=limit).execute()
        records = []
        for item in result.get("messages", []):
            message = service.users().messages().get(
                userId="me", id=item["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            headers_map = {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}
            sender = headers_map.get("from", "Unknown sender")
            subject = headers_map.get("subject", "(no subject)")
            preview = message.get("snippet", "")
            category = _email_category(subject, preview)
            requires_reply = any(term in f"{subject} {preview}".lower() for term in ("please reply", "please respond", "let me know", "can you", "could you", "rsvp"))
            try:
                received = datetime.fromtimestamp(int(message.get("internalDate", "0")) / 1000, tz=timezone.utc).isoformat()
            except (ValueError, OSError):
                received = headers_map.get("date", "")
            gmail_url = f"https://mail.google.com/mail/u/0/#all/{item['id']}"
            row = db.scalar(select(models.EmailMetadata).where(models.EmailMetadata.gmail_message_id == item["id"]))
            values = {"sender": sender, "subject": subject, "received_at": received, "preview": preview, "category": category, "requires_reply": requires_reply, "gmail_url": gmail_url, "updated_at": datetime.now(timezone.utc)}
            if row:
                for key, value in values.items(): setattr(row, key, value)
            else:
                row = models.EmailMetadata(gmail_message_id=item["id"], **values)
                db.add(row)
            records.append(row)
        db.commit()
        return {"messages": records, "count": len(records), "source": "Gmail API unread messages", "ai_summaries": False}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Gmail read failed (%s)", type(exc).__name__)
        raise HTTPException(502, "Could not read Gmail. Check Google API access and reconnect if authorization expired.") from exc


@app.get("/api/briefing", dependencies=[Depends(authorize)])
def briefing(db: Session = Depends(get_db)):
    tasks = db.scalars(select(models.Task).where(models.Task.completed.is_(False)).order_by(models.Task.priority.desc())).all()
    lines = ["Good morning. Here's your current plan:"]
    lines.extend(f"• {t.title}" + (f" (due {t.due_date})" if t.due_date else "") for t in tasks[:8])
    if not tasks: lines.append("No open tasks yet. Add a task to start planning your day.")
    lines.append("Calendar and inbox are not connected yet; no events or emails were retrieved.")
    return {"text": "\n".join(lines), "tasks": len(tasks), "timezone": settings.timezone}
