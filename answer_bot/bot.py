import os
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from openai import AsyncOpenAI

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("otvet")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
AI_API_KEY = os.environ.get("AI_API_KEY", "")
AI_BASE_URL = os.environ.get("AI_BASE_URL") or None
AI_MODEL = os.environ.get("AI_MODEL", "gpt-4o-mini")
client = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL) if AI_API_KEY else None

SYSTEM = """Ты — «Ответь», умный помощник по перепискам. Анализируй контекст и пиши естественно, по-русски, без канцелярита и шаблонных фраз. Не выдумывай факты о людях. Предлагай три реально разные версии: 1) спокойно и уважительно, 2) уверенно и с границами, 3) живо, с юмором или теплом по ситуации. Если пользователь указал стиль, следуй ему. Кратко объясняй подтекст только когда это полезно. Не выдавай догадки о чувствах собеседника за факты."""

STYLE_LABELS = {"calm":"Спокойно", "confident":"Уверенно", "warm":"Тепло", "flirt":"Флирт", "funny":"С юмором", "business":"По-деловому"}
STYLE_PROMPTS = {"calm":"Ответь спокойно и доброжелательно.", "confident":"Ответь уверенно, с достоинством и ясными границами.", "warm":"Ответь тепло и искренне.", "flirt":"Ответь легко, игриво и флиртующе, без навязчивости.", "funny":"Добавь уместный юмор и живую речь.", "business":"Ответь коротко, профессионально и по делу."}

def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🙂 Спокойно", callback_data="style:calm"), InlineKeyboardButton("💪 Уверенно", callback_data="style:confident")],
        [InlineKeyboardButton("❤️ Тепло", callback_data="style:warm"), InlineKeyboardButton("😉 Флирт", callback_data="style:flirt")],
        [InlineKeyboardButton("😂 С юмором", callback_data="style:funny"), InlineKeyboardButton("💼 По-деловому", callback_data="style:business")],
        [InlineKeyboardButton("🔎 Разобрать смысл", callback_data="analyze")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я «Ответь» — тестовый ИИ-помощник по перепискам.\n\n"
        "Пришли текст сообщения или вставь часть диалога. Я предложу три варианта ответа. "
        "Можешь переслать сообщение из другого чата или выбрать стиль кнопками.\n\n"
        "Пока работаю в тестовом режиме; не отправляю ответы собеседникам за тебя.",
        reply_markup=menu()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Пришли текст переписки. Для выбора тона нажми кнопку под сообщением. Команды: /start, /reset.")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Контекст сброшен. Пришли новое сообщение 👌", reply_markup=menu())

async def ask_ai(text: str, style: str = "", analyze: bool = False):
    if not client:
        return "⚠️ Нейросеть ещё не подключена. В Render добавь AI_API_KEY и перезапусти сервис."
    instruction = STYLE_PROMPTS.get(style, "")
    if analyze:
        instruction += " Сначала кратко разбери возможный смысл и тон сообщения, отделяя факты от предположений, затем предложи три ответа."
    try:
        response = await client.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role":"system","content":SYSTEM + "\n" + instruction},
                      {"role":"user","content":text[:12000]}],
            temperature=0.75,
            max_tokens=900
        )
        return response.choices[0].message.content or "Не получилось сформировать ответ. Попробуй ещё раз."
    except Exception:
        log.exception("AI request failed")
        return "Не удалось получить ответ от нейросети. Проверь ключ, модель и настройки API."

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    context.user_data["last_text"] = msg.text
    await msg.reply_text("Секунду, разбираю переписку…")
    result = await ask_ai(msg.text, context.user_data.get("style", ""))
    await msg.reply_text(result[:4000], reply_markup=menu())

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not client:
        await msg.reply_text("Для голосовых нужно подключить AI_API_KEY в настройках сервиса.")
        return
    try:
        voice = msg.voice or msg.audio
        file = await context.bot.get_file(voice.file_id)
        path = f"/tmp/voice_{msg.message_id}.ogg"
        await file.download_to_drive(path)
        with open(path, "rb") as audio:
            tr = await client.audio.transcriptions.create(model=os.getenv("STT_MODEL","whisper-1"), file=audio)
        transcript = (tr.text or "").strip()
        if not transcript:
            await msg.reply_text("Не удалось разобрать голосовое. Попробуй отправить ещё раз.")
            return
        context.user_data["last_text"] = transcript
        result = await ask_ai("Текст голосового сообщения:\n" + transcript, context.user_data.get("style", ""))
        await msg.reply_text("🎙 Распознал: " + transcript[:1200])
        await msg.reply_text(result[:4000], reply_markup=menu())
    except Exception:
        log.exception("Voice processing failed")
        await msg.reply_text("Не получилось обработать голосовое. Проверь настройки API или пришли текстом.")

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data.startswith("style:"):
        style = data.split(":",1)[1]
        context.user_data["style"] = style
        await q.message.reply_text(f"Стиль выбран: {STYLE_LABELS.get(style, style)}. Теперь пришли сообщение или нажми «Разобрать смысл».")
    elif data == "analyze":
        text = context.user_data.get("last_text")
        if not text:
            await q.message.reply_text("Сначала пришли сообщение, которое нужно разобрать.")
        else:
            await q.message.reply_text(await ask_ai(text, context.user_data.get("style",""), True), reply_markup=menu())

telegram_app = Application.builder().token(BOT_TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("help", help_cmd))
telegram_app.add_handler(CommandHandler("reset", reset))
telegram_app.add_handler(CallbackQueryHandler(buttons))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
telegram_app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))

@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)
    log.info("Answer bot polling started")
    yield
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()

app = FastAPI(title="Ответь test bot", lifespan=lifespan)

@app.get("/")
async def root():
    return {"service":"Ответь","mode":"test","status":"ok","ai_configured":bool(client)}

@app.get("/health")
async def health():
    return {"status":"ok"}
