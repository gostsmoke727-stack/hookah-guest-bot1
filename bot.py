import asyncio
import csv
import os
import random
import re
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message
from faster_whisper import WhisperModel

BASE = Path(__file__).resolve().parent
CSV_PATH = BASE / "data" / "assortment.csv"

KEYWORDS = {
    "слад": "СЛАДКИЙ", "сладко": "СЛАДКИЙ",
    "кисл": "КИСЛЫЙ", "кисло-слад": "КИСЛО-СЛАДКИЙ",
    "свеж": "СВЕЖИЙ", "прян": "ПРЯНЫЙ",
    "нейтр": "НЕЙТРАЛЬНЫЙ", "солен": "СОЛЕНЫЙ",
    "фрукт": "Фрукты", "ягод": "Ягодные", "ягодн": "Ягодные",
    "цитрус": "Цитрусовые", "десерт": "Десертные", "десер": "Десертные",
    "трав": "Травяные", "цвет": "Цветочные", "спец": "Специи",
    "напит": "Напитки", "чай": "Чайные",
    "креп": "Крепкие", "крепкий": "Крепкие",
    "легк": "Легкие", "мягк": "Легкие", "средн": "Средние",
}

STRENGTH_WORDS = {
    "лег": "Легкие", "мяг": "Легкие",
    "сред": "Средние", "креп": "Крепкие", "силь": "Крепкие",
}

BOWL_WORDS = {
    "арбуз": "Арбуз", "дын": "Дыня", "грейп": "Грейпфрут",
    "ананас": "Ананас", "гранат": "Гранат",
    "класс": "Классическая чаша", "обыч": "Классическая чаша",
    "классичес": "Классическая чаша",
}

FREQUENCY_WORDS = (
    "каждый день", "каждый вечер", "ежедневно", "часто", "регулярно",
    "раз в неделю", "раза в неделю", "несколько раз в неделю",
    "раз в месяц", "редко", "иногда", "пару раз", "первый раз",
)

def load_assortment():
    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("Бренд") and r.get("Название")]

ASSORTMENT = load_assortment()

# Small, CPU-friendly model. It supports Russian and keeps the bot self-contained.
WHISPER_MODEL = WhisperModel(
    os.getenv("WHISPER_MODEL", "base"),
    device="cpu",
    compute_type="int8",
)

sessions = {}


def get_session(user_id):
    return sessions.setdefault(user_id, {"step": 1, "answers": {}})


def reset_session(user_id):
    sessions[user_id] = {"step": 1, "answers": {}}
    return sessions[user_id]


def normalize(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def detect_strength(text):
    t = normalize(text)
    for word, value in STRENGTH_WORDS.items():
        if word in t:
            return value
    return None


def detect_bowl(text):
    t = normalize(text)
    for word, value in BOWL_WORDS.items():
        if word in t:
            return value
    return None


def detect_frequency(text):
    t = normalize(text)
    for phrase in FREQUENCY_WORDS:
        if phrase in t:
            return t
    return t


def candidates(profile):
    taste = normalize(profile["taste"])
    dislikes = normalize(profile["dislikes"])
    strength = profile.get("strength")
    t = f"{taste} {profile.get('frequency', '')}".lower()

    wanted = {v for k, v in KEYWORDS.items() if k in t}
    disliked_words = [w for w in re.findall(r"[а-яёa-z0-9-]+", dislikes) if len(w) >= 3]

    scored = []
    for row in ASSORTMENT:
        category = normalize(row.get("Категория", ""))
        direction = normalize(row.get("Направление", ""))
        row_strength = normalize(row.get("Крепость", ""))
        name = normalize(row.get("Название", ""))
        description = normalize(row.get("Описание", ""))

        score = 0

        for field in (category, direction, row_strength):
            if any(normalize(w) in field or field in normalize(w) for w in wanted):
                score += 3

        if strength and normalize(strength) in row_strength:
            score += 4

        if strength == "Легкие" and any(x in row_strength for x in ("легк", "мяг")):
            score += 3
        if strength == "Средние" and "сред" in row_strength:
            score += 3
        if strength == "Крепкие" and ("креп" in row_strength or "креп" in category):
            score += 3

        for word in re.findall(r"[а-яёa-z0-9-]+", taste):
            if len(word) >= 4 and (word in name or word in description or word in direction):
                score += 2

        if any(word in name or word in description or word in category or word in direction
               for word in disliked_words):
            score -= 10

        if score > 0:
            scored.append((score, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored]


def make_variants(rows):
    rows = rows[:15]
    random.shuffle(rows)
    if len(rows) < 6:
        return None

    groups = [rows[0:3], rows[3:6], rows[6:9]]
    names = ["Сочный микс", "Яркий баланс", "Необычный микс"]
    out = []

    for name, group in zip(names, groups):
        lines = [f"🔥 Вариант — «{name}»"]
        for r in group:
            lines.append(f'• {r["Бренд"]} — {r["Название"]}')
        out.append("\n".join(lines))

    return "\n\n".join(out)


async def transcribe_voice(bot, message):
    voice = message.voice
    tg_file = await bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        path = tmp.name

    try:
        await bot.download(tg_file, destination=path)
        segments, _ = WHISPER_MODEL.transcribe(
            path,
            language="ru",
            vad_filter=True,
            beam_size=5,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return text
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def get_user_text(bot, message):
    if message.text:
        return message.text.strip()

    if message.voice:
        await message.answer("🎙️ Секунду, распознаю голосовое…")
        text = await transcribe_voice(bot, message)
        if not text:
            await message.answer("Не получилось разобрать голосовое. Запиши ещё раз или напиши текстом.")
            return None
        return text

    return None


async def ask_step(message, step):
    if step == 1:
        await message.answer(
            "Что бы ты хотел(а) сегодня покурить? 🎯\n\n"
            "Можешь написать или записать голосовое. Например: сладкое, ягодное, "
            "свежее, кислое, фруктовое, десертное, пряное — или просто опиши вкус своими словами."
        )
    elif step == 2:
        await message.answer(
            "Как часто ты куришь кальян и какую крепость предпочитаешь? 💨\n\n"
            "Можно ответить одним сообщением или голосовым: редко / иногда / часто / "
            "каждую неделю и лёгкий / средний / крепкий."
        )
    elif step == 3:
        await message.answer(
            "Есть вкусы или ингредиенты, которые ты не любишь или точно не хочешь? "
            "И какую чашу выбираем? 🍉\n\n"
            "Можно на фруктовой: арбуз, дыня, грейпфрут, ананас или гранат — "
            "либо на классической чаше. Ответь текстом или голосовым."
        )


async def finish_recommendation(message, profile):
    rows = candidates(profile)
    answer = make_variants(rows)

    if not answer:
        # Fallback: use all assortment but still respect explicit dislikes where possible.
        rows = [r for r in ASSORTMENT if not any(
            word in normalize(r.get("Название", "")) or
            word in normalize(r.get("Описание", "")) or
            word in normalize(r.get("Направление", ""))
            for word in re.findall(r"[а-яёa-z0-9-]+", normalize(profile["dislikes"]))
            if len(word) >= 3
        )]
        answer = make_variants(rows)

    bowl = profile.get("bowl", "Классическая чаша")
    strength = profile.get("strength") or "по предпочтению гостя"

    if answer:
        await message.answer(
            "Подобрал варианты под твой запрос. 🔥\n\n"
            f"{answer}\n\n"
            f"🍉 Чаша: {bowl}\n"
            f"💪 Крепость: {strength}\n\n"
            "Если хочешь, могу подобрать ещё варианты — слаще, кислее, свежее или крепче."
        )
    else:
        await message.answer(
            "В точном сочетании не нашёл достаточно вариантов в текущем ассортименте. "
            "Могу предложить ближайшие по вкусу варианты — напиши «ещё»."
        )

    sessions.pop(message.from_user.id, None)


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
            "Привет! 👋\n"
            "Я помогу подобрать кальян именно под твой вкус. "
            "Можно отвечать обычным текстом или голосовыми сообщениями."
        )
        await ask_step(message, 1)

    @dp.message(F.text | F.voice)
    async def dialog(message: Message):
        user_id = message.from_user.id
        session = get_session(user_id)
        text = await get_user_text(bot, message)

        if not text:
            return

        step = session["step"]

        if step == 1:
            session["answers"]["taste"] = text
            session["step"] = 2
            await ask_step(message, 2)
            return

        if step == 2:
            session["answers"]["frequency"] = detect_frequency(text)
            session["answers"]["strength"] = detect_strength(text)
            session["step"] = 3
            await ask_step(message, 3)
            return

        if step == 3:
            session["answers"]["dislikes"] = text
            session["answers"]["bowl"] = detect_bowl(text) or "Классическая чаша"
            await finish_recommendation(message, session["answers"])

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
