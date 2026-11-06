from __future__ import annotations

import time
import secrets
import logging
import base64
import hashlib
from pathlib import Path

from fastapi import HTTPException

from ..config import settings

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_COMPOSE_SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
CALENDAR_WRITE_SCOPES = ["https://www.googleapis.com/auth/calendar.events.owned"]
GOOGLE_SCOPES = GMAIL_SCOPES + GMAIL_COMPOSE_SCOPES + CALENDAR_SCOPES + CALENDAR_WRITE_SCOPES
CALLBACK_URI = "http://127.0.0.1:8000/api/gmail/oauth/callback"
# Keep PKCE's verifier with its one-time state until Google's callback arrives.
# The verifier is transient, never logged, and expires with the state.
OAUTH_STATES: dict[str, tuple[float, str | None, str]] = {}
logger = logging.getLogger("jarvis.oauth")


def backend_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return path


def credentials_file() -> Path:
    return backend_path(settings.google_client_secrets_file)


def token_file() -> Path:
    return backend_path(settings.google_token_file)


def gmail_connected() -> bool:
    path = token_file()
    if not path.exists():
        return False
    try:
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(str(path))
        return creds.has_scopes(GMAIL_SCOPES) and (creds.valid or bool(creds.refresh_token))
    except Exception:
        return False


def gmail_drafts_connected() -> bool:
    path = token_file()
    if not path.exists():
        return False
    try:
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(str(path))
        return creds.has_scopes(GMAIL_COMPOSE_SCOPES) and (creds.valid or bool(creds.refresh_token))
    except Exception:
        return False


def make_oauth_flow(state: str, code_verifier: str | None = None):
    from google_auth_oauthlib.flow import Flow

    path = credentials_file()
    if not path.exists():
        raise HTTPException(
            status_code=503,
            detail=f"Google OAuth client file is missing: {path.name}. Follow the Gmail setup instructions in README.md.",
        )
    try:
        flow = Flow.from_client_secrets_file(
            str(path),
            scopes=GOOGLE_SCOPES,
            state=state,
            code_verifier=code_verifier,
            autogenerate_code_verifier=code_verifier is None,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Google OAuth client JSON is invalid or is not a Web application client.") from exc
    flow.redirect_uri = CALLBACK_URI
    return flow


def create_authorization_url(return_to: str = "gmail") -> str:
    if return_to not in {"gmail", "calendar"}:
        raise ValueError("Unsupported OAuth return destination")
    state = secrets.token_urlsafe(32)
    for old_state, (expires, _, _) in list(OAUTH_STATES.items()):
        if expires < time.time():
            OAUTH_STATES.pop(old_state, None)
    flow = make_oauth_flow(state)
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    OAUTH_STATES[state] = (time.time() + 600, flow.code_verifier, return_to)
    return authorization_url


def cancel_authorization(state: str) -> str:
    record = OAUTH_STATES.pop(state, None)
    if record is None or record[0] < time.time():
        return "gmail"
    return record[2]


def exchange_authorization_code(state: str, code: str) -> str:
    state_record = OAUTH_STATES.pop(state, None)
    if state_record is None or state_record[0] < time.time():
        raise HTTPException(status_code=400, detail="OAuth state is invalid or expired. Start the Gmail connection again.")
    flow = make_oauth_flow(state, code_verifier=state_record[1])
    try:
        flow.fetch_token(code=code)
        creds = flow.credentials
        if not creds.has_scopes(GMAIL_SCOPES):
            raise HTTPException(status_code=403, detail="Gmail read permission was not granted.")
        path = token_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(creds.to_json(), encoding="utf-8")
        return state_record[2]
    except HTTPException:
        raise
    except Exception as exc:
        # OAuthlib's exception includes Google's error code and sometimes its
        # description. Log only those safe fields; never log the callback URL,
        # authorization code, client secret, or raw response body.
        oauth_error = str(getattr(exc, "error", "") or "").lower()
        oauth_description = str(getattr(exc, "description", "") or "").lower()
        reason = f"{oauth_error} {oauth_description} {type(exc).__name__}".lower()
        logger.warning(
            "Google OAuth token exchange failed: category=%s exception=%s",
            oauth_error or "unknown",
            type(exc).__name__,
        )
        if "invalid_grant" in reason or "expired" in reason or "already been redeemed" in reason:
            detail = "Google rejected the one-time authorization code. Start Connect Gmail again and complete the consent flow without reusing an old callback URL."
        elif "redirect_uri_mismatch" in reason:
            detail = f"Google reports a redirect URI mismatch. The Web client must authorize this exact URI: {CALLBACK_URI}"
        elif "invalid_client" in reason or "unauthorized_client" in reason:
            detail = "Google rejected this OAuth client. Confirm credentials.json belongs to the Web application client created for this JARVIS project."
        elif "connection" in reason or "timeout" in reason:
            detail = "JARVIS could not reach Google's token service. Check the backend internet connection and retry Gmail authorization."
        else:
            detail = "Google authorization could not be completed. Start Connect Gmail again; if it fails, check the backend terminal for the safe error category."
        raise HTTPException(status_code=400, detail=detail) from exc


def build_gmail_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    path = token_file()
    if not path.exists():
        raise HTTPException(status_code=401, detail="Gmail is not connected. Use Connect Gmail first.")
    try:
        creds = Credentials.from_authorized_user_file(str(path))
        if not creds.has_scopes(GMAIL_SCOPES):
            raise HTTPException(status_code=403, detail="Saved Google authorization lacks Gmail read permission. Reconnect Gmail.")
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            path.write_text(creds.to_json(), encoding="utf-8")
        if not creds.valid:
            raise HTTPException(status_code=401, detail="Gmail authorization expired. Reconnect Gmail.")
        return build("gmail", "v1", credentials=creds, cache_discovery=False)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Could not refresh Gmail authorization. Reconnect Gmail.") from exc


def calendar_connected() -> bool:
    path = token_file()
    if not path.exists():
        return False
    try:
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(str(path))
        return creds.has_scopes(CALENDAR_SCOPES) and (creds.valid or bool(creds.refresh_token))
    except Exception:
        return False


def calendar_write_connected() -> bool:
    path = token_file()
    if not path.exists():
        return False
    try:
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(str(path))
        return creds.has_scopes(CALENDAR_WRITE_SCOPES) and (creds.valid or bool(creds.refresh_token))
    except Exception:
        return False


def build_calendar_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    path = token_file()
    if not path.exists():
        raise HTTPException(status_code=401, detail="Google is not connected. Connect Calendar first.")
    try:
        creds = Credentials.from_authorized_user_file(str(path))
        if not creds.has_scopes(CALENDAR_SCOPES):
            raise HTTPException(status_code=403, detail="Calendar read permission was not granted. Reconnect Google and allow Calendar access.")
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            path.write_text(creds.to_json(), encoding="utf-8")
        if not creds.valid:
            raise HTTPException(status_code=401, detail="Google authorization expired. Reconnect Calendar.")
        return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Could not refresh Calendar authorization. Reconnect Google.") from exc


def create_gmail_draft(recipient: str, subject: str, body: str) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from email.message import EmailMessage

    path = token_file()
    if not path.exists():
        raise HTTPException(401, "Gmail is not connected. Connect Gmail first.")
    try:
        creds = Credentials.from_authorized_user_file(str(path))
        if not creds.has_scopes(GMAIL_COMPOSE_SCOPES):
            raise HTTPException(403, "Email draft permission is not granted yet. Reconnect Google from Gmail Intelligence and allow Gmail draft access.")
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            path.write_text(creds.to_json(), encoding="utf-8")
        if not creds.valid:
            raise HTTPException(401, "Google authorization expired. Reconnect Gmail.")
        message = EmailMessage()
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        result = service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        return str(result.get("id", ""))
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Gmail draft creation failed (%s)", type(exc).__name__)
        raise HTTPException(502, "Gmail could not save the draft. Confirm Gmail API access and reconnect if authorization expired.") from exc


def create_calendar_event(approval_id: int, event_data: dict) -> dict:
    from datetime import date, timedelta
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    path = token_file()
    if not path.exists():
        raise HTTPException(401, "Google is not connected. Connect Calendar first.")
    try:
        creds = Credentials.from_authorized_user_file(str(path))
        if not creds.has_scopes(CALENDAR_WRITE_SCOPES):
            raise HTTPException(403, "Calendar event creation permission is missing. Reconnect Google and allow event creation access.")
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            path.write_text(creds.to_json(), encoding="utf-8")
        if not creds.valid:
            raise HTTPException(401, "Google authorization expired. Reconnect Calendar.")
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        event_id = hashlib.sha256(f"jarvis-approval-{approval_id}".encode()).hexdigest()[:32]
        if event_data.get("all_day"):
            event_date = date.fromisoformat(event_data["date"])
            event = {
                "id": event_id,
                "summary": event_data["summary"],
                "description": event_data.get("description", ""),
                "start": {"date": event_date.isoformat()},
                "end": {"date": (event_date + timedelta(days=1)).isoformat()},
            }
        else:
            event = {
            "id": event_id,
            "summary": event_data["summary"],
            "description": event_data.get("description", ""),
            "start": {"dateTime": event_data["start"], "timeZone": event_data["timezone"]},
            "end": {"dateTime": event_data["end"], "timeZone": event_data["timezone"]},
            }
        result = service.events().insert(calendarId="primary", body=event).execute()
        return {"id": result.get("id", event_id), "html_link": result.get("htmlLink", "")}
    except HTTPException:
        raise
    except Exception as exc:
        if getattr(getattr(exc, "resp", None), "status", None) == 409:
            try:
                existing = service.events().get(calendarId="primary", eventId=event_id).execute()
                return {"id": existing.get("id", event_id), "html_link": existing.get("htmlLink", "")}
            except Exception:
                pass
        # If an earlier request succeeded but its response was lost, the stable
        # event ID lets us avoid silently creating a second calendar entry.
        logger.warning("Google Calendar event creation failed (%s)", type(exc).__name__)
        raise HTTPException(502, "Calendar could not create the approved event. Check Calendar API access and authorization, then retry the approval.") from exc


def list_calendar_events(day):
    from datetime import datetime, time, timedelta
    from zoneinfo import ZoneInfo

    from ..config import settings

    tz = ZoneInfo(settings.timezone)
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    try:
        response = (
            build_calendar_service()
            .events()
            .list(
                calendarId="primary",
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                maxResults=50,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Google Calendar read failed (%s)", type(exc).__name__)
        raise HTTPException(502, "Could not read Calendar. Confirm the Google Calendar API is enabled, then reconnect Calendar.") from exc

    items = []
    for event in response.get("items", []):
        event_start = event.get("start", {})
        event_end = event.get("end", {})
        items.append({
            "id": event.get("id", ""),
            "summary": event.get("summary") or "(No title)",
            "start": event_start.get("dateTime") or event_start.get("date"),
            "end": event_end.get("dateTime") or event_end.get("date"),
            "all_day": "date" in event_start and "dateTime" not in event_start,
            "location": event.get("location", ""),
            "html_link": event.get("htmlLink", ""),
        })

    def as_datetime(value: str):
        if len(value) == 10:
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(tz)

    conflicts = []
    timed = [item for item in items if not item["all_day"]]
    for index, first in enumerate(timed):
        first_start, first_end = as_datetime(first["start"]), as_datetime(first["end"])
        for second in timed[index + 1:]:
            second_start, second_end = as_datetime(second["start"]), as_datetime(second["end"])
            if first_start < second_end and second_start < first_end:
                conflicts.append({"first": first["summary"], "second": second["summary"]})
    return {"events": items, "conflicts": conflicts, "date": day.isoformat(), "timezone": settings.timezone}
