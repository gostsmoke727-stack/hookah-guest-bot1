# Ответь — тестовая сборка

Telegram AI assistant for drafting replies and analyzing conversation snippets.

## Environment variables
- TELEGRAM_BOT_TOKEN (required)
- AI_API_KEY (required for AI)
- AI_BASE_URL (optional, for OpenAI-compatible providers)
- AI_MODEL (default: gpt-4o-mini)
- STT_MODEL (default: whisper-1)

## Run
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 10000

Test mode: it drafts replies only; it never sends messages to a user's contacts. Do not send highly sensitive personal data. Messages and voice transcripts are sent to the configured AI provider for processing.
