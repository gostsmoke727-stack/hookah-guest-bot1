import asyncio
import base64
import csv
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message

BASE = Path(__file__).resolve().parent
CSV_PATH = BASE / "data" / "assortment.csv"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
LOCAL_WHISPER_MODEL = os.getenv("WHISPER_MODEL", "tiny")

with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as f:
    ASSORTMENT = [r for r in csv.DictReader(f) if r.get("Бренд") and r.get("Название")]

sessions = {}
_whisper_model = None

CATEGORIES = [
    "СЛАДКИЙ", "КИСЛЫЙ", "КИСЛО-СЛАДКИЙ", "СВЕЖИЙ", "ПРЯНЫЙ",
    "НЕЙТРАЛЬНЫЙ", "СОЛЕНЫЙ", "Фрукты", "Ягодные", "Цитрусовые",
    "Десертные", "Травяные", "Цветочные", "Специи", "Напитки", "Чайные", "Крепкие"
]
BOWLS = ["Арбуз", "Дыня", "Грейпфрут", "Ананас", "Гранат", "Классическая чаша"]

AI_SYSTEM = """
Ты — AI-администратор кальянного заведения. Общайся по-русски живо, коротко и естественно,
как хороший кальянный мастер: без длинных анкет и канцелярита.

Твоя задача — понять гостя и собрать профиль для подбора из реального ассортимента.

КРИТИЧЕСКОЕ ПРАВИЛО: различай "хочу" и "не хочу".
Фразы "не люблю ананас", "без ананаса", "ананас не надо",
"аллергия на ананас" НИКОГДА не записывай как желаемый вкус.
Аллергии и явные запреты — абсолютные исключения.

Понимай разговорные формулировки:
"ягоды с тропиками", "что-нибудь свежее", "не приторное", "покрепче",
"мягко", "часто курю", "редко курю", "на классике", "на фрукте".
"Классика/классическая чаша" = классическая чаша.

Если гость говорит несколько пожеланий в одном сообщении, извлеки все сразу.
Не выдумывай наличие вкуса или ингредиента. Реальные позиции выбираются отдельно из CSV.

На каждом ходе верни ТОЛЬКО JSON:
{
  "reply": "короткий естественный ответ гостю",
  "desired_terms": ["ягоды", "тропики"],
  "desired_categories": ["Ягодные", "Фрукты"],
  "excluded_terms": ["ананас", "маракуйя"],
  "allergies": [],
  "bowl": "Классическая чаша",
  "strength": "Средние",
  "frequency": "иногда",
  "ready": false,
  "missing": ["крепость"],
  "clarifying_question": "Какую крепость предпочитаешь — лёгкую, среднюю или покрепче?"
}

Обновляй поля с учётом ВСЕЙ истории и текущего профиля, не затирай уже найденные ограничения.
Если информации достаточно — ready=true и missing=[].
Если не хватает только несущественной детали, не мучай гостя вопросами: ready=true.
Максимум один короткий вопрос за ход.
"""

def norm(s):
    return re.sub(r"\s+", " ", str(s or "").strip().lower())

def empty_profile():
    return {
        "desired_terms": [],
        "desired_categories": [],
        "excluded_terms": [],
        "allergies": [],
        "bowl": None,
        "strength": None,
        "frequency": None,
    }

def get_session(user_id):
    return sessions.setdefault(user_id, {
        "profile": empty_profile(),
        "history": [],
        "turns": 0,
    })

def reset_session(user_id):
    sessions[user_id] = {"profile": empty_profile(), "history": [], "turns": 0}
    return sessions[user_id]

def merge_unique(old, new):
    out = list(old or [])
    for item in new or []:
        item = str(item).strip()
        if item and norm(item) not in {norm(x) for x in out}:
            out.append(item)
    return out

def merge_profile(old, data):
    p = dict(old)
    for key in ("desired_terms", "desired_categories", "excluded_terms", "allergies"):
        p[key] = merge_unique(p.get(key), data.get(key))
    for key in ("bowl", "strength", "frequency"):
        value = data.get(key)
        if value and str(value).strip().lower() not in ("null", "none"):
            p[key] = value
    return p

def openrouter_request(user_prompt):
    if not OPENROUTER_API_KEY:
        return None
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": AI_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.15,
        "max_tokens": 350,
        "response_format": {"type": "json_object"},
    }
    url = "https://openrouter.ai/api/v1/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + OPENROUTER_API_KEY,
            "HTTP-Referer": "https://github.com/gostsmoke727-stack/hookah-guest-bot1",
            "X-Title": "Hookah Guest Bot",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            raw = response.read().decode("utf-8")
        data = json.loads(raw)
        text = data["choices"][0]["message"]["content"]
        return json.loads(text)
    except Exception:
        return None

async def ai_understand(session, user_text=None, audio_bytes=None, mime_type="audio/ogg"):
    profile_json = json.dumps(session["profile"], ensure_ascii=False)
    history = json.dumps(session["history"][-8:], ensure_ascii=False)
    context = (
        "ТЕКУЩИЙ ПРОФИЛЬ:\\n" + profile_json +
        "\\nИСТОРИЯ ДИАЛОГА:\\n" + history +
        "\\nСделай следующий ход диалога."
    )
    if audio_bytes is not None:
        return None
    prompt = context + "\\nСООБЩЕНИЕ ГОСТЯ:\\n" + (user_text or "")
    return await asyncio.to_thread(openrouter_request, prompt)

def load_local_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel(
            LOCAL_WHISPER_MODEL,
            device="cpu",
            compute_type="int8",
        )
    return _whisper_model

async def local_transcribe(path):
    model = await asyncio.to_thread(load_local_whisper)
    segments, _ = await asyncio.to_thread(
        model.transcribe, path, language="ru", vad_filter=True, beam_size=1
    )
    return " ".join(s.text.strip() for s in segments).strip()

async def transcribe_voice_fallback(bot, message):
    tg_file = await bot.get_file(message.voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        path = tmp.name
    try:
        await bot.download(tg_file, destination=path)
        return await local_transcribe(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

def term_matches(row, term):
    t = norm(term)
    if not t:
        return False
    fields = [
        norm(row.get("Название", "")),
        norm(row.get("Описание", "")),
        norm(row.get("Направление", "")),
        norm(row.get("Категория", "")),
    ]
    aliases = {
        "маракуйя": ["маракуйя", "пассифлора", "passion fruit"],
        "ананас": ["ананас", "pineapple"],
        "ягоды": ["ягод", "клубник", "малина", "черник", "смородин", "ежевик", "вишн", "черешн"],
        "тропики": ["троп", "манго", "маракуй", "ананас", "кокос", "папай", "личи", "гуав"],
        "цитрус": ["цитрус", "лимон", "лайм", "апельс", "грейп", "мандар"],
        "свежее": ["свеж", "мята", "холод", "ice"],
        "сладкое": ["слад", "десерт"],
    }
    needles = aliases.get(t, [t])
    return any(any(n in field for n in needles) for field in fields)

def score_candidates(profile):
    desired_terms = profile.get("desired_terms", [])
    desired_categories = profile.get("desired_categories", [])
    excluded = merge_unique(profile.get("excluded_terms"), profile.get("allergies"))
    strength = norm(profile.get("strength"))

    scored = []
    for row in ASSORTMENT:
        # Hard exclusions ALWAYS win.
        if any(term_matches(row, term) for term in excluded):
            continue

        score = 0
        category = norm(row.get("Категория", ""))
        direction = norm(row.get("Направление", ""))
        row_strength = norm(row.get("Крепость", ""))

        for wanted in desired_categories:
            w = norm(wanted)
            if w and (w in category or w in direction):
                score += 8

        for wanted in desired_terms:
            if term_matches(row, wanted):
                score += 7

        if strength:
            if strength in row_strength:
                score += 7
            elif strength.startswith("лег") and "лег" in row_strength:
                score += 6
            elif strength.startswith("сред") and "сред" in row_strength:
                score += 6
            elif strength.startswith("креп") and "креп" in row_strength:
                score += 6

        # A result with no positive taste match is not allowed unless the guest
        # gave no taste preference at all.
        if desired_terms or desired_categories:
            if score < 7:
                continue

        scored.append((score, row))

    scored.sort(key=lambda x: (-x[0], norm(x[1].get("Название", ""))))
    return [row for _, row in scored]

def make_recommendations(rows, limit=3):
    selected = []
    used_brands = set()
    for row in rows:
        brand = row.get("Бренд", "")
        if len(selected) < limit and brand not in used_brands:
            selected.append(row)
            used_brands.add(brand)
    if len(selected) < limit:
        for row in rows:
            if row not in selected:
                selected.append(row)
                if len(selected) >= limit:
                    break
    return selected[:limit]

def build_result(profile, rows):
    bowl = profile.get("bowl") or "Классическая чаша"
    strength = profile.get("strength") or "Средняя"
    lines = [
        "🔥 Подобрал под твой запрос:",
        f"🍉 Чаша: {bowl}",
        f"💪 Крепость: {strength}",
        "",
    ]
    for i, row in enumerate(make_recommendations(rows), 1):
        lines.append(f"{i}. {row['Бренд']} — {row['Название']}")
    if profile.get("excluded_terms") or profile.get("allergies"):
        blocked = merge_unique(profile.get("excluded_terms"), profile.get("allergies"))
        lines.append("")
        lines.append("🚫 Исключил: " + ", ".join(blocked))
    lines.append("")
    lines.append("Если хочешь — можем докрутить вкус: слаще, свежее, кислее или крепче.")
    return "\\n".join(lines)

def local_profile_from_text(text):
    # Safety fallback only when AI key is unavailable.
    t = norm(text)
    p = empty_profile()
    p["desired_terms"] = [x for x in ["ягоды", "тропики", "сладкое", "кислое", "свежее"] if x in t]
    p["excluded_terms"] = re.findall(
        r"(?:не хочу|не люблю|без|не надо|аллергия на)\\s+([а-яёa-z-]+)", t
    )
    for bowl in BOWLS:
        if norm(bowl) in t or (bowl == "Классическая чаша" and "класс" in t):
            p["bowl"] = bowl
    if "креп" in t or "покрепче" in t:
        p["strength"] = "Крепкие"
    elif "легк" in t or "мяг" in t:
        p["strength"] = "Легкие"
    elif "сред" in t:
        p["strength"] = "Средние"
    return p

async def handle_turn(bot, message, user_text=None, audio_bytes=None):
    session = get_session(message.from_user.id)
    session["turns"] += 1

    ai = await ai_understand(session, user_text=user_text, audio_bytes=audio_bytes)
    if ai:
        session["profile"] = merge_profile(session["profile"], ai)
        reply = (ai.get("reply") or "").strip()
        ready = bool(ai.get("ready"))
        missing = ai.get("missing") or []

        # Never allow the model to recommend an item itself.
        if not ready and session["turns"] < 4:
            question = (ai.get("clarifying_question") or reply).strip()
            if question:
                session["history"].append({"role": "user", "text": user_text or "[голосовое]"})
                session["history"].append({"role": "assistant", "text": question})
                await message.answer(question)
                return
        rows = score_candidates(session["profile"])
        if rows:
            await message.answer(build_result(session["profile"], rows))
        else:
            await message.answer(
                "Я понял запрос, но в текущем ассортименте нет позиции, которая одновременно "
                "подходит по вкусу и не нарушает твои запреты. Давай чуть изменим вкус — "
                "например, добавим соседнее ягодное или фруктовое направление."
            )
        sessions.pop(message.from_user.id, None)
        return

    # AI unavailable: keep the bot usable with local rules.
    if user_text:
        fallback = local_profile_from_text(user_text)
        session["profile"] = merge_profile(session["profile"], fallback)
        rows = score_candidates(session["profile"])
        if rows:
            await message.answer(build_result(session["profile"], rows))
            sessions.pop(message.from_user.id, None)
            return
    await message.answer(
        "Сейчас AI недоступен. Напиши пожелания текстом ещё раз — я попробую подобрать из ассортимента."
    )

async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")

    bot = Bot(token)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(message: Message):
        reset_session(message.from_user.id)
        await message.answer(
            "Привет! 👋 Я помогу подобрать кальян под твой вкус. "
            "Можно писать или отправлять голосовые.\n\n"
            "Что сегодня хочется покурить? Опиши вкус как удобно — "
            "например, ягоды, тропики, сладкое, свежее, кислое или что-то своё."
        )

    @dp.message(F.voice)
    async def voice(message: Message):
        if OPENROUTER_API_KEY:
            await message.answer("🎙️ Слушаю…")
            tg_file = await bot.get_file(message.voice.file_id)
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                path = tmp.name
            try:
                await bot.download(tg_file, destination=path)
                with open(path, "rb") as f:
                    audio = f.read()
                text = await local_transcribe(path)
                if text:
                    await handle_turn(bot, message, user_text=text)
                else:
                    await message.answer("Не разобрал голосовое. Попробуй ещё раз или напиши текстом.")
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        else:
            await message.answer("🎙️ Секунду…")
            text = await transcribe_voice_fallback(bot, message)
            if text:
                await handle_turn(bot, message, user_text=text)
            else:
                await message.answer("Не разобрал голосовое. Попробуй ещё раз или напиши текстом.")

    @dp.message(F.text)
    async def text_message(message: Message):
        await handle_turn(bot, message, user_text=message.text.strip())

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
