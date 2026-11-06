from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from .. import models
from ..agent.actions import execute_approval_action, execute_explicit_chat_action, follows_calendar_approval_prompt, is_confirmation_only
from ..config import settings
from ..database import SessionLocal
from ..integrations.google_gmail import calendar_connected, list_calendar_events
from ..integrations.telegram_bot import configured_bot

logger = logging.getLogger("jarvis.telegram")


def _set_approval(db, approval: models.Approval, decision: str) -> str:
    action_result = execute_approval_action(approval) if decision == "approve" else "Approval rejected; no external action was taken."
    approval.status = decision
    approval.decided_at = datetime.now(timezone.utc)
    db.add(models.ActionLog(
        action=approval.action,
        result=action_result,
    ))
    db.commit()
    return action_result


def _format_tasks(db) -> str:
    tasks = db.scalars(
        select(models.Task)
        .where(models.Task.completed.is_(False))
        .order_by(models.Task.due_date, models.Task.created_at)
        .limit(12)
    ).all()
    if not tasks:
        return "No open tasks."
    reminders = db.scalars(select(models.TaskReminder).where(models.TaskReminder.sent_at.is_(None))).all()
    reminder_by_task = {item.task_id: item.scheduled_at for item in reminders}
    def local_time(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(ZoneInfo(settings.timezone)).strftime("%b %d %I:%M %p")
    return "\n".join(
        f"• {task.title} [{task.priority}]" + (f" — due {task.due_date}" if task.due_date else "") + (f" — reminder {local_time(reminder_by_task[task.id])} {settings.timezone}" if task.id in reminder_by_task else "")
        for task in tasks
    )


async def _briefing(db) -> str:
    lines = ["Your JARVIS briefing", "", "Open tasks:", _format_tasks(db)]
    if calendar_connected():
        try:
            today = datetime.now(ZoneInfo(settings.timezone)).date()
            data = await asyncio.to_thread(list_calendar_events, today)
            lines.extend(["", "Today's calendar:"])
            if data["events"]:
                for event in data["events"][:10]:
                    lines.append(f"• {event['start']} — {event['summary']}")
            else:
                lines.append("No events today.")
            if data["conflicts"]:
                lines.append("Schedule conflicts: " + "; ".join(f"{c['first']} overlaps {c['second']}" for c in data["conflicts"]))
        except HTTPException:
            lines.extend(["", "Calendar could not be retrieved right now."])
    else:
        lines.extend(["", "Calendar is not connected."])
    urgent = db.scalars(
        select(models.EmailMetadata)
        .where(models.EmailMetadata.category == "Urgent")
        .order_by(models.EmailMetadata.updated_at.desc())
        .limit(5)
    ).all()
    if urgent:
        lines.extend(["", "Urgent unread email previews:"])
        lines.extend(f"• {mail.subject} — {mail.sender}" for mail in urgent)
    return "\n".join(lines)


async def _chat_reply(text: str) -> str:
    db = SessionLocal()
    try:
        conversation = db.scalar(select(models.Conversation).where(models.Conversation.title == "Telegram assistant").order_by(models.Conversation.id).limit(1))
        if conversation is None:
            conversation = models.Conversation(title="Telegram assistant")
            db.add(conversation)
            db.commit()
            db.refresh(conversation)
        history = db.scalars(
            select(models.Message)
            .where(models.Message.conversation_id == conversation.id)
            .order_by(models.Message.created_at.desc())
            .limit(10)
        ).all()
        db.add(models.Message(conversation_id=conversation.id, role="user", content=text))
        db.commit()
        if is_confirmation_only(text) and follows_calendar_approval_prompt(history):
            answer = "I haven't added anything from that reply. Calendar events must appear in JARVIS Approvals first, and only /approve <id> or the Approve button there can add them."
        else:
            answer = await execute_explicit_chat_action(text, db)
        if answer is None:
            answer = "Gemini is not configured. Add GEMINI_API_KEY to backend/.env to enable Telegram chat."
        if answer is None or (answer.startswith("Gemini is not configured") and settings.gemini_api_key):
            try:
                from google import genai

                prompt = ("You are JARVIS, the user's highly professional, capable personal assistant. Address the user respectfully as Sir in a natural way, without repeating it in every sentence. Be polished, concise, calm, proactive, and courteous. Be honest and never claim success without confirmation. "
                          "This app can create explicitly requested tasks, send a scheduled task reminder to this private Telegram when configured, save explicitly requested Gmail drafts when draft access is connected (never send them), and create Google Calendar events only after approval. It cannot control the phone or computer's native calendar/alarm. Never claim to have set a reminder unless the task action confirms it; reminders do not send email. "
                          "Consequential external actions require explicit approval.\n")
                memories = db.scalars(select(models.Memory).limit(20)).all()
                if memories:
                    prompt += "User preferences: " + "; ".join(item.content for item in memories) + "\n"
                turns = [f"{item.role}: {item.content}" for item in reversed(history)]
                turns.append(f"user: {text}")
                client = genai.Client(api_key=settings.gemini_api_key)
                try:
                    response = await asyncio.to_thread(
                        client.models.generate_content,
                        model=settings.gemini_model,
                        contents=prompt + "\n".join(turns),
                    )
                    answer = response.text or "I couldn't produce a response. Please try again."
                finally:
                    client.close()
            except Exception as exc:
                logger.warning("Telegram Gemini reply failed (%s)", type(exc).__name__)
                answer = "I couldn't reach Gemini just now. Please try again shortly."
        db.add(models.Message(conversation_id=conversation.id, role="assistant", content=answer))
        db.commit()
        return answer
    finally:
        db.close()


async def handle_telegram_message(text: str) -> str:
    bot = configured_bot()
    if bot is None:
        return "Telegram is not configured on the JARVIS backend."
    db = SessionLocal()
    try:
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        if command == "/start":
            return "JARVIS is connected. Send me a message, or use /help to see what I can do."
        if command == "/help":
            return "JARVIS commands:\n/briefing - today's tasks and calendar\n/tasks - open tasks\n/schedule - today's calendar\n/pending - pending approvals\n/approve <id> - record approval\n/reject <id> - reject an approval"
        if command == "/tasks":
            return _format_tasks(db)
        if command == "/briefing":
            return await _briefing(db)
        if command == "/schedule":
            if not calendar_connected():
                return "Google Calendar is not connected yet. Connect it from JARVIS → Schedule."
            try:
                today = datetime.now(ZoneInfo(settings.timezone)).date()
                data = await asyncio.to_thread(list_calendar_events, today)
                if not data["events"]:
                    return f"No events today ({data['timezone']})."
                lines = [f"Today's schedule ({data['timezone']}):"]
                lines.extend(f"• {event['start']} — {event['summary']}" for event in data["events"])
                if data["conflicts"]:
                    lines.append("Conflicts: " + "; ".join(f"{item['first']} overlaps {item['second']}" for item in data["conflicts"]))
                return "\n".join(lines)
            except HTTPException as exc:
                return exc.detail
        if command == "/pending":
            approvals = db.scalars(select(models.Approval).where(models.Approval.status == "pending").order_by(models.Approval.created_at).limit(10)).all()
            if not approvals:
                return "No pending approvals."
            return "Pending approvals:\n" + "\n".join(f"#{item.id} {item.action} — {item.target}\n{item.details[:180]}" for item in approvals)
        if command in {"/approve", "/reject"}:
            try:
                approval_id = int(argument.strip().split()[0])
            except (ValueError, IndexError):
                return f"Use {command} <approval_id>. See /pending for IDs."
            approval = db.get(models.Approval, approval_id)
            if approval is None:
                return f"Approval #{approval_id} was not found."
            if approval.status != "pending":
                return f"Approval #{approval_id} was already {approval.status}."
            decision = "approve" if command == "/approve" else "reject"
            try:
                action_result = _set_approval(db, approval, decision)
            except HTTPException as exc:
                return f"Approval #{approval_id} remains pending: {exc.detail}"
            if decision == "approve":
                return f"Approval #{approval_id} marked approved. {action_result}"
            return f"Approval #{approval_id} rejected."
    finally:
        db.close()
    return await _chat_reply(text)


async def telegram_update_handler(update: dict) -> None:
    bot = configured_bot()
    if bot is None:
        return
    text = await handle_telegram_message(update["text"])
    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        await bot.send_message(client, text)


async def telegram_polling_loop() -> None:
    bot = configured_bot()
    if bot is None:
        return
    logger.info("Telegram polling enabled for the configured private user")
    await bot.run(telegram_update_handler)
