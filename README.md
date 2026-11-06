# JARVIS Personal AI

A local-first personal assistant MVP built with FastAPI, SQLite/SQLAlchemy, Gemini and React/Vite. The dashboard stores tasks, conversation history, memories, and approval decisions in your database. Integrations that need account credentials are clearly marked until configured.

## Requirements

- Python 3.13 (the existing `backend/venv` is preserved)
- Node.js 20 or newer and npm
- Optional Gemini API key for AI responses

## Local setup

1. In `backend`, copy `.env.example` to `.env`. Set `GEMINI_API_KEY` to enable chat. Set `DASHBOARD_API_KEY` and the same `VITE_DASHBOARD_API_KEY` in `frontend/.env` if you want dashboard API authentication. Keep the API key private; the web frontend key is suitable only for a local, single-user development environment.
2. Install backend dependencies with `backend\venv\Scripts\python.exe -m pip install -r backend\requirements.txt` from the repository root, or activate `backend\venv` first and run `pip install -r requirements.txt` inside `backend`.
3. Start the API from `backend`: `venv\Scripts\python.exe -m uvicorn app.main:app --reload`.
4. In another terminal, run `npm install` and `npm run dev` from `frontend`.
5. Open http://localhost:5173. API docs are at http://localhost:8000/docs.

The SQLite database (`backend/jarvis.db` when started from `backend`) is created automatically. PostgreSQL URLs are supported with `DATABASE_URL=postgresql+psycopg://...`.

## Configuration

See `backend/.env.example` and `frontend/.env.example`. No credentials are committed. `GEMINI_API_KEY` is available from [Google AI Studio](https://aistudio.google.com/app/apikey). The default model is `gemini-3.5-flash-lite` and is configurable with `GEMINI_MODEL`. Without a Gemini key, chat saves messages and reports that AI is not configured rather than fabricating a response. Web research is not enabled; the UI and API indicate this clearly.

## Integrations

- **Telegram:** Private-user bot polling and persistent task reminders are implemented. Set `TELEGRAM_BOT_TOKEN` and your numeric `TELEGRAM_USER_ID` in `backend/.env`, restart the backend, then open the bot and send `/start`. JARVIS supports `/help`, `/briefing`, `/tasks`, `/schedule`, `/pending`, `/approve <id>`, `/reject <id>`, and Gemini-backed natural-language actions. Task reminders are delivered to this private Telegram chat; other Telegram user IDs are silently ignored.
- **Gmail:** Read-only inbox intelligence and user-requested email drafts are supported. Enable the Gmail API, configure OAuth testing users, and create a **Web application** OAuth client with `http://127.0.0.1:8000/api/gmail/oauth/callback` as an authorized redirect URI. Save its JSON as `backend/credentials.json`. Authorize `gmail.readonly` and `gmail.compose` from **Gmail Intelligence**. Chat or Telegram can prepare a draft on explicit request; JARVIS never sends email. Public use of Gmail scopes may require Google verification. See [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) and [Google's web server OAuth flow](https://developers.google.com/identity/protocols/oauth2/web-server).
- **Calendar:** JARVIS reads today's primary-calendar events, detects overlaps, and can create an event only after explicit approval in Approvals or Telegram `/approve <id>`. Enable the Calendar API in the same Cloud project, then authorize `calendar.readonly` and `calendar.events.owned` from **Schedule**. The existing OAuth client and callback URI are reused.
- **Approvals:** Decisions and execution results are logged. Approved Calendar proposals create events; other unsupported external actions remain recorded without execution.

## Security and deployment

`.env`, database files, `credentials.json`, and `token.json` are ignored. Do not expose this development server publicly: `DASHBOARD_API_KEY` sent by a browser is not a substitute for production user authentication. Before deployment, add proper authentication, HTTPS, secret-manager backed credentials, restrictive CORS, database migrations/backups, and a durable scheduler/worker. Set a managed PostgreSQL `DATABASE_URL`; deploy frontend and backend separately and set `VITE_API_URL` / `FRONTEND_ORIGIN`. Scheduled automation, Telegram, Gmail, Calendar, and cloud-ready authentication are follow-up implementation work, not currently operational features.

## Current MVP features

- Responsive dashboard with overview, chat, tasks, schedule/Gmail status, approvals, memory, and settings pages.
- Persistent task creation, completion, deletion, and progress stats.
- Persistent chat history and Gemini-backed replies when configured.
- User-managed persistent memory included as context in Gemini prompts.
- Natural-language task creation in chat and Telegram, explicit Gmail draft creation, and calendar event proposals executed only after approval.
- Persisted Asia/Kolkata task reminder times delivered to the configured private Telegram user; reminders prompt the user and never send email.
- Approval queue with one-time decisions and action decision logs.
- Briefing endpoint based on open tasks and explicit indication that inbox/calendar are not connected.
- Asia/Kolkata configuration and PostgreSQL-compatible SQLAlchemy URL.
