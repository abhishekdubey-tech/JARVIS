import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models
from app.agent.actions import (
    AssistantIntent,
    _fallback_intent,
    _looks_like_action,
    execute_explicit_chat_action,
    follows_calendar_approval_prompt,
    is_confirmation_only,
)
from app.database import Base
from app.config import settings


class AssistantActionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_explicit_task_request_persists_task(self):
        intent = AssistantIntent(action="task", title="Review notes", due_date="2026-11-04")
        with patch("app.agent.actions._interpret", new=AsyncMock(return_value=intent)):
            answer = asyncio.run(execute_explicit_chat_action("Add a task to review notes tomorrow", self.session))
        task = self.session.scalar(select(models.Task))
        self.assertEqual(task.title, "Review notes")
        self.assertEqual(task.due_date, "2026-11-04")
        self.assertIn("Added task", answer)

    def test_calendar_request_only_creates_pending_approval(self):
        intent = AssistantIntent(
            action="calendar_event", title="Project review",
            start_datetime="2026-11-05T15:00:00", end_datetime="2026-11-05T15:30:00",
        )
        with patch("app.agent.actions._interpret", new=AsyncMock(return_value=intent)):
            answer = asyncio.run(execute_explicit_chat_action("Schedule project review", self.session))
        approval = self.session.scalar(select(models.Approval))
        self.assertEqual(approval.status, "pending")
        self.assertEqual(approval.action, "calendar.create_event")
        self.assertIn("only after you approve", answer)

    def test_birthday_request_creates_all_day_pending_approval(self):
        intent = AssistantIntent(
            action="calendar_event", title="Happy Birthday Messi",
            event_date="2026-12-08", all_day=True,
        )
        with patch("app.agent.actions._interpret", new=AsyncMock(return_value=intent)):
            answer = asyncio.run(execute_explicit_chat_action(
                "jarvis baby create an event of happy birthday messi on 8 dec", self.session
            ))
        approval = self.session.scalar(select(models.Approval))
        self.assertEqual(approval.status, "pending")
        self.assertEqual(approval.target, "Happy Birthday Messi")
        self.assertIn('"date": "2026-12-08"', approval.details)
        self.assertIn("Review it in Approvals", answer)

    def test_short_confirmation_after_calendar_approval_offer_does_not_trigger_action(self):
        history = [
            models.Message(role="user", content="Create an event on my calendar"),
            models.Message(role="assistant", content="I will queue it for your approval."),
        ]
        self.assertTrue(is_confirmation_only("yeah"))
        self.assertTrue(follows_calendar_approval_prompt(history))
        self.assertTrue(_looks_like_action("jarvis baby create an event of happy birthday messi on 8 dec"))

    def test_requested_email_draft_calls_draft_api_only(self):
        intent = AssistantIntent(
            action="email_draft", recipient="person@example.com", subject="Follow up",
            draft_body="Hello, following up on our conversation.",
        )
        with patch("app.agent.actions._interpret", new=AsyncMock(return_value=intent)), patch(
            "app.agent.actions.create_gmail_draft", return_value="draft-123"
        ) as create_draft:
            answer = asyncio.run(execute_explicit_chat_action("Draft an email", self.session))
        create_draft.assert_called_once_with(
            recipient="person@example.com", subject="Follow up",
            body="Hello, following up on our conversation.",
        )
        self.assertIn("has not been sent", answer)

    def test_reminder_phrase_extracts_task_and_time(self):
        intent = _fallback_intent("ok add a task of 8:20 pm today to send this draft and notify me")
        self.assertEqual(intent.action, "task")
        self.assertEqual(intent.title.lower(), "send this draft")
        self.assertTrue(intent.reminder_at.endswith("T20:20:00+05:30") or "T20:20:00+" in intent.reminder_at)

    def test_task_reminder_is_persisted_for_private_telegram_delivery(self):
        now = datetime.now(ZoneInfo(settings.timezone)) + timedelta(hours=1)
        intent = AssistantIntent(
            action="task", title="Send the draft", due_date=now.date().isoformat(),
            reminder_at=now.isoformat(),
        )
        with patch("app.agent.actions._interpret", new=AsyncMock(return_value=intent)), patch.object(
            settings, "telegram_bot_token", "configured-test-token"
        ), patch.object(settings, "telegram_user_id", "12345"):
            answer = asyncio.run(execute_explicit_chat_action("Add a task to send the draft in one hour", self.session))
        task = self.session.scalar(select(models.Task))
        reminder = self.session.scalar(select(models.TaskReminder))
        self.assertEqual(reminder.task_id, task.id)
        self.assertIsNone(reminder.sent_at)
        self.assertIn("on Telegram", answer)
        self.assertIn("send the email", answer)

    def test_due_reminder_is_sent_once_and_marked(self):
        task = models.Task(title="Send the draft")
        self.session.add(task)
        self.session.commit()
        self.session.refresh(task)
        reminder = models.TaskReminder(
            task_id=task.id, message=task.title,
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        self.session.add(reminder)
        self.session.commit()
        factory = sessionmaker(bind=self.engine)
        with patch("app.services.reminder_service.SessionLocal", factory), patch(
            "app.services.reminder_service.configured_bot", return_value=object()
        ), patch(
            "app.services.reminder_service.send_private_telegram_message", new=AsyncMock(return_value=True)
        ) as send:
            from app.services.reminder_service import deliver_due_reminders_once
            delivered = asyncio.run(deliver_due_reminders_once())
        check = factory().get(models.TaskReminder, reminder.id)
        self.assertEqual(delivered, 1)
        self.assertIsNotNone(check.sent_at)
        send.assert_awaited_once_with("JARVIS reminder: Send the draft")


if __name__ == "__main__":
    unittest.main()
