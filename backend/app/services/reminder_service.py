from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from .. import models
from ..database import SessionLocal
from ..integrations.telegram_bot import configured_bot, send_private_telegram_message

logger = logging.getLogger("jarvis.reminders")


async def deliver_due_reminders_once() -> int:
    """Deliver persisted due task reminders; return the number delivered."""
    if configured_bot() is None:
        return 0
    db = SessionLocal()
    sent = 0
    try:
        cutoff = datetime.now(timezone.utc)
        due = db.scalars(
            select(models.TaskReminder)
            .join(models.Task, models.Task.id == models.TaskReminder.task_id)
            .where(
                models.TaskReminder.sent_at.is_(None),
                models.TaskReminder.scheduled_at <= cutoff,
                models.Task.completed.is_(False),
            )
            .order_by(models.TaskReminder.scheduled_at)
            .limit(20)
        ).all()
        for reminder in due:
            task = db.get(models.Task, reminder.task_id)
            if task is None or task.completed:
                continue
            message = f"JARVIS reminder: {reminder.message}"
            if await send_private_telegram_message(message):
                reminder.sent_at = datetime.now(timezone.utc)
                db.commit()
                sent += 1
        return sent
    except Exception as exc:
        db.rollback()
        logger.warning("Reminder delivery cycle failed (%s)", type(exc).__name__)
        return sent
    finally:
        db.close()


async def reminder_worker() -> None:
    logger.info("Persistent task reminder worker started")
    while True:
        try:
            await deliver_due_reminders_once()
            await asyncio.sleep(20)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Reminder worker recovered from %s", type(exc).__name__)
            await asyncio.sleep(20)
