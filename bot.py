import asyncio
import csv
import os
import random
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message

BASE = Path(__file__).resolve().parent
CSV_PATH = BASE / "data" / "assortment.csv"

KEYWORDS = {
    "слад": "СЛАДКИЙ", "сладко": "СЛАДКИЙ", "кисл": "КИСЛЫЙ",
    "кисло-слад": "КИСЛО-СЛАДКИЙ", "свеж": "СВЕЖИЙ", "прян": "ПРЯНЫЙ",
    "нейтр": "НЕЙТРАЛЬНЫЙ", "солен": "СОЛЕНЫЙ",
    "фрукт": "Фрукты", "ягод": "Ягодные", "цитрус": "Цитрусовые",
    "десерт": "Десертные", "десер": "Десертные", "трав": "Травяные",
    "цвет": "Цветочные", "спец": "Специи", "напит": "Напитки",
    "чай": "Чайные", "креп": "Крепкие", "крепкий": "Крепкие", "легк": "Легкие", "мягк": "Легкие",
    "средн": "Средние", "ягодн": "Ягодные",
}


def load_assortment():
    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("Бренд") and r.get("Название")]

ASSORTMENT = load_assortment()


def candidates(text: str):
    t = text.lower()
    wanted = {v for k, v in KEYWORDS.items() if k in t}
    if not wanted:
        return []
    scored = []
    for row in ASSORTMENT:
        score = 0
        for field in ("Категория", "Крепость", "Направление"):
            value = row.get(field, "")
            if value in wanted or (value == "Крепкий" and "Крепкие" in wanted) or (value == "Десерты" and "Десертные" in wanted) or (value == "Ягоды" and "Ягодные" in wanted):
                score += 3
        if any(w in row.get("Название", "").lower() for w in t.split() if len(w) >= 4):
            score += 1
        if score:
            scored.append((score, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored]


def make_variants(rows):
    rows = rows[:15]
    random.shuffle(rows)
    if len(rows) < 6:
        return []
    groups = [rows[0:3], rows[3:6], rows[6:9]]
    names = ["Сочный микс", "Яркий баланс", "Необычный микс"]
    out = []
    for name, group in zip(names, groups):
        lines = [f'🔥 Вариант — «{name}»']
        for r in group:
            lines.append(f'• {r["Бренд"]} — {r["Название"]}')
        out.append("\n".join(lines))
    return "\n\n".join(out)


async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")
    bot = Bot(token)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(message: Message):
        await message.answer(
            "Привет! Я помогу подобрать кальян из нашего ассортимента.\n"
            "Напиши, что хочется: сладкое, кислое, свежее, ягодное, фруктовое, десертное и т.д."
        )

    @dp.message(F.text)
    async def recommend(message: Message):
        rows = candidates(message.text or "")
        answer = make_variants(rows)
        if not answer:
            await message.answer(
                "Чтобы подобрать точно, скажи максимум 2 вещи:\n"
                "1) какой вкус — сладкий / кислый / свежий / пряный?\n"
                "2) какое направление — фрукты / ягоды / цитрус / десерт / напиток?"
            )
            return
        await message.answer(answer + "\n\nМогу сделать ещё 3 — слаще / кислее / свежее / крепче.")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
