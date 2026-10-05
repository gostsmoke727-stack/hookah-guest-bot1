import asyncio
import csv
import html
import json
import os
import re
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery

BASE = Path(__file__).resolve().parent
CSV_PATH = BASE / "data" / "assortment.csv"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").replace("\ufeff", "").strip().strip("\"").strip("'").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "tiny")

with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as f:
    ASSORTMENT = [r for r in csv.DictReader(f) if r.get("Бренд") and r.get("Название")]

sessions = {}
_whisper_model = None

CURATED_PAIRINGS = [
    {"name":"Raspberry + Pinkman + Grapefruit","terms":["малина","pinkman","грейпфрут"],"ratio":["30%","30%","40%"],"source":"Hookah House"},
    {"name":"Strawberry Jam + Guava","terms":["клубничный джем","гуава"],"ratio":["70%","30%"],"source":"Fumari"},
    {"name":"Watermelon + Guava","terms":["арбуз","гуава"],"ratio":["50%","50%"],"source":"Fumari"},
    {"name":"Watermelon + Strawberry Jam","terms":["арбуз","клубничный джем"],"ratio":["60%","40%"],"source":"Fumari"},
    {"name":"Strawberry Jam + Banana Custard","terms":["клубничный джем","банан"],"ratio":["40%","60%"],"source":"Fumari"},
    {"name":"Strawberry Jam + Purple Grape + Watermelon","terms":["клубничный джем","виноград","арбуз"],"ratio":["40%","40%","20%"],"source":"Fumari"},
    {"name":"Watermelon + Guava + Mint","terms":["арбуз","гуава","мята"],"ratio":["30%","40%","30%"],"source":"Fumari"},
    {"name":"Strawberry Jam + Limoncello + White Peach","terms":["клубничный джем","лимончелло","белый персик"],"ratio":["20%","60%","20%"],"source":"Fumari"},
    {"name":"Watermelon + Mojito + Strawberry Jam","terms":["арбуз","мохито","клубничный джем"],"ratio":["20%","30%","50%"],"source":"Fumari"},
]

AI_SYSTEM = """
Ты — AI-мастер кальяна премиального заведения.
Не проводи анкету. Веди короткий естественный диалог и запоминай гостя.

КРИТИЧЕСКИЕ ПРАВИЛА:
1. "ХОЧУ" и "НЕ ХОЧУ" — разные сущности.
2. Любой явный запрет, аллергия или "не люблю" — абсолютный запрет.
3. Не выдумывай вкусы, бренды и наличие. Позиции выбирает отдельный движок.
4. Не задавай лишних вопросов, если данных достаточно.
5. Максимум один короткий вопрос за ход.
6. Учитывай всю историю и сохранённую память.
7. Отвечай по-русски живо, коротко, без анкет и канцелярита.
8. Если данных достаточно, в reply коротко отреагируй на гостя, но не придумывай факты о составе/наличии.
9. Не перегружай гостя: максимум одна короткая мысль и один вопрос за ход.

Понимай:
"не приторное" -> снижай сладость;
"свежее" -> свежесть/мята/холод;
"без мяты", "не люблю холодок" -> абсолютный запрет;
"ягоды с тропиками" -> две положительные оси;
"на классике" -> классическая чаша;
"покрепче/полегче" -> крепость;
"как в прошлый раз" -> память;
"удиви меня" -> один знакомый и один экспериментальный вариант.

Верни ТОЛЬКО JSON:
{
 "reply": "короткая фраза",
 "name": "",
 "desired_terms": [],
 "desired_categories": [],
 "excluded_terms": [],
 "allergies": [],
 "bowl": null,
 "strength": null,
 "sweetness": null,
 "freshness": null,
 "frequency": null,
 "mood": null,
 "style": null,
 "ready": false,
 "missing": [],
 "clarifying_question": ""
}
"""

def norm(s):
    return re.sub(r"\s+", " ", str(s or "").strip().lower())

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def empty_profile():
    return {
        "desired_terms": [], "desired_categories": [], "excluded_terms": [],
        "allergies": [], "bowl": None, "strength": None,
        "sweetness": None, "freshness": None, "frequency": None, "mood": None
    }

def empty_memory(user_id, message):
    return {
        "user_id": user_id,
        "name": (message.from_user.first_name or "").strip(),
        "username": (message.from_user.username or "").strip(),
        "style": "дружелюбно",
        "likes": [], "dislikes": [], "allergies": [],
        "usual_strength": None, "usual_bowl": None,
        "favorite_flavors": [], "last_hookahs": [],
        "visit_count": 0, "last_seen": now_iso()
    }

def merge_unique(old, new):
    out = list(old or [])
    for item in new or []:
        item = str(item).strip()
        if item and norm(item) not in {norm(x) for x in out}:
            out.append(item)
    return out

def as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return norm(value) in {"true", "1", "yes", "да"}
    return False

def merge_profile(old, data):
    p = dict(old)
    for key in ("desired_terms", "desired_categories", "excluded_terms", "allergies"):
        p[key] = merge_unique(p.get(key), data.get(key))
    blocked = {norm(x) for x in p["excluded_terms"] + p["allergies"]}
    p["desired_terms"] = [x for x in p["desired_terms"] if norm(x) not in blocked]
    for key in ("bowl", "strength", "sweetness", "freshness", "frequency", "mood"):
        value = data.get(key)
        if value and norm(value) not in {"null", "none"}:
            p[key] = value
    return p

def supabase_request(method, path, body=None, params=""):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    req = urllib.request.Request(
        SUPABASE_URL + path + params,
        data=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": "Bearer " + SUPABASE_KEY,
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else True
    except Exception:
        return None

def load_memory(user_id, message):
    fallback = empty_memory(user_id, message)
    if not SUPABASE_URL or not SUPABASE_KEY:
        return fallback
    rows = supabase_request("GET", "/rest/v1/guest_memory",
                             params=f"?user_id=eq.{int(user_id)}&select=*")
    return rows[0] if isinstance(rows, list) and rows else fallback

def save_memory(memory):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    supabase_request("POST", "/rest/v1/guest_memory", body=memory,
                     params="?on_conflict=user_id")

def update_memory(memory, data, profile, recommendations=None, increment_visit=False):
    if data.get("name"):
        memory["name"] = str(data["name"]).strip()
    if data.get("style"):
        memory["style"] = str(data["style"]).strip()
    memory["likes"] = merge_unique(memory.get("likes"),
                                    profile["desired_terms"] + profile["desired_categories"])
    memory["dislikes"] = merge_unique(memory.get("dislikes"), profile["excluded_terms"])
    memory["allergies"] = merge_unique(memory.get("allergies"), profile["allergies"])
    if profile.get("strength"):
        memory["usual_strength"] = profile["strength"]
    if profile.get("bowl"):
        memory["usual_bowl"] = profile["bowl"]
    if recommendations:
        memory["last_hookahs"] = [
            r["Бренд"] + " — " + r["Название"] for r in recommendations[:3]
        ]
    memory["favorite_flavors"] = merge_unique(
        memory.get("favorite_flavors"), profile["desired_terms"]
    )
    if increment_visit:
        memory["visit_count"] = int(memory.get("visit_count") or 0) + 1
    memory["last_seen"] = now_iso()
    save_memory(memory)

def profile_from_memory(memory):
    return merge_profile(empty_profile(), {
        "desired_terms": memory.get("favorite_flavors", []) or memory.get("likes", []),
        "desired_categories": [],
        "excluded_terms": memory.get("dislikes", []),
        "allergies": memory.get("allergies", []),
        "bowl": memory.get("usual_bowl"), "strength": memory.get("usual_strength"),
    })

def openrouter_request(system_prompt, user_prompt):
    if not OPENROUTER_API_KEY:
        return None
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 280,
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode(),
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
            data = json.loads(response.read().decode())
        text = str(data["choices"][0]["message"]["content"]).strip()
        fence = chr(96) * 3
        text = re.sub(r"^" + re.escape(fence) + r"(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*" + re.escape(fence) + r"$", "", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, flags=re.S)
            return json.loads(match.group(0)) if match else None
    except Exception:
        return None

async def ai_understand(session, user_text):
    prompt = (
        "ПАМЯТЬ ГОСТЯ:\n" + json.dumps(session["memory"], ensure_ascii=False) +
        "\nТЕКУЩИЙ ПРОФИЛЬ:\n" + json.dumps(session["profile"], ensure_ascii=False) +
        "\nИСТОРИЯ:\n" + json.dumps(session["history"][-10:], ensure_ascii=False) +
        "\nСООБЩЕНИЕ ГОСТЯ:\n" + user_text
    )
    return await asyncio.to_thread(openrouter_request, AI_SYSTEM, prompt)

def canonical_term(term):
    t = norm(term)
    aliases = {"маракую":"маракуйя","маракуя":"маракуйя","пассифлору":"пассифлора","трофимов":"trofimoff's","трофимоф":"trofimoff's","trofimoff":"trofimoff's"}
    return aliases.get(t, t)

def term_matches(row, term):
    t = canonical_term(term)
    fields = [norm(row.get(k, "")) for k in
              ("Бренд", "Название", "Описание", "Направление", "Категория")]
    aliases = {
        "маракуйя": ["маракуй", "маракуя", "пассифлор", "passion fruit"],
        "trofimoff's": ["trofimoff", "трофимов", "трофимоф"],
        "pinkman": ["pinkman", "пинкман"],
        "клубничный джем": ["клубничный джем", "strawberry jam"],
        "гуава": ["гуава", "guava"],
        "виноград": ["виноград", "grape"],
        "банан": ["банан", "banana"],
        "лимончелло": ["лимончелло", "limoncello"],
        "белый персик": ["белый персик", "white peach", "персик"],
        "мохито": ["мохито", "mojito"],
        "ананас": ["ананас", "pineapple"],
        "мята": ["мят", "mint", "menthol", "ice"],
        "холод": ["холод", "ice", "ментол"],
        "ягоды": ["ягод", "клубник", "малин", "черник", "смород",
                  "ежев", "вишн", "черешн", "брусник", "крыжов"],
        "тропики": ["троп", "манго", "маракуй", "ананас", "кокос",
                    "папай", "личи", "гуав", "банан"],
        "цитрус": ["цитрус", "лимон", "лайм", "апельс", "грейп",
                   "мандар", "помело", "юдзу", "yuzu"],
        "свежее": ["свеж", "мята", "холод", "ice", "ментол"],
        "сладкое": ["слад", "десерт", "ванил", "крем", "сахар"],
        "кислое": ["кисл", "цитрус", "лимон", "лайм"],
    }
    needles = aliases.get(t, [t])
    return any(any(n in field for n in needles) for field in fields)

def score_candidates(profile, memory):
    desired_terms = profile["desired_terms"]
    desired_categories = profile["desired_categories"]
    excluded = merge_unique(profile["excluded_terms"], profile["allergies"])
    strength = norm(profile.get("strength"))
    soft_likes = memory.get("favorite_flavors", []) + memory.get("likes", [])
    scored = []

    for row in ASSORTMENT:
        if any(term_matches(row, x) for x in excluded):
            continue
        score = 0
        matches = 0
        category = norm(row.get("Категория", ""))
        direction = norm(row.get("Направление", ""))
        row_strength = norm(row.get("Крепость", ""))

        for wanted in desired_categories:
            if norm(wanted) and (norm(wanted) in category or norm(wanted) in direction):
                score += 8
                matches += 1
        for wanted in desired_terms:
            if term_matches(row, wanted):
                score += 10
                matches += 1
        for wanted in soft_likes:
            if term_matches(row, wanted):
                score += 2
        if strength:
            if strength in row_strength:
                score += 6
            elif strength.startswith("лег") and "лег" in row_strength:
                score += 5
            elif strength.startswith("сред") and "сред" in row_strength:
                score += 5
            elif strength.startswith("креп") and "креп" in row_strength:
                score += 5

        sweetness = norm(profile.get("sweetness"))
        if "не" in sweetness and "слад" in sweetness and "слад" in category:
            score -= 5
        freshness = norm(profile.get("freshness"))
        if ("без" in freshness or "не" in freshness) and term_matches(row, "свежее"):
            score -= 8

        if (desired_terms or desired_categories) and matches == 0:
            continue
        scored.append((score, matches, row))

    scored.sort(key=lambda x: (-x[0], -x[1], norm(x[2]["Название"])))
    return [row for _, _, row in scored]


def find_curated_pairing(profile, rows):
    blocked = merge_unique(profile.get("excluded_terms"), profile.get("allergies"))
    desired = profile.get("desired_terms", []) + profile.get("desired_categories", [])
    candidates = []
    for pairing in CURATED_PAIRINGS:
        resolved = []
        used = set()
        ok = True
        for term in pairing["terms"]:
            found = [r for r in rows if term_matches(r, term) and not any(term_matches(r, b) for b in blocked)]
            found = [r for r in found if (r["Бренд"], r["Название"]) not in used]
            if not found:
                ok = False
                break
            found.sort(key=lambda r: (norm(r.get("Крепость","")) != "средние", norm(r["Бренд"]), norm(r["Название"])))
            row = found[0]
            used.add((row["Бренд"], row["Название"]))
            resolved.append(row)
        if not ok:
            continue
        if norm(profile.get("freshness")).startswith(("без", "не")) and any(term_matches(r, "мята") for r in resolved):
            continue
        score = sum(5 for wanted in desired if any(term_matches(r, wanted) for r in resolved))
        candidates.append((score, pairing, resolved))
    if not candidates:
        return None
    candidates.sort(key=lambda x: -x[0])
    return candidates[0][1], candidates[0][2]

def build_pairing_text(profile, rows):
    result = find_curated_pairing(profile, rows)
    if not result:
        return ""
    pairing, resolved = result
    parts = []
    for i, row in enumerate(resolved):
        ratio = pairing["ratio"][i] if i < len(pairing["ratio"]) else ""
        parts.append(f"{ratio} {html.escape(row['Бренд'])} — {html.escape(row['Название'])}".strip())
    return ("<b>🎯 Готовое сочетание</b>\n" + " + ".join(parts) +
            f"\n<i>Основа: опубликованный микс {html.escape(pairing['source'])}; "
            "показываю его только когда все компоненты найдены в текущем ассортименте.</i>")

def make_recommendations(rows, limit=3, exclude_names=None):
    exclude_names = {norm(x) for x in (exclude_names or [])}
    selected, brands = [], set()
    for row in rows:
        if norm(row["Бренд"] + " — " + row["Название"]) in exclude_names:
            continue
        if row["Бренд"] not in brands:
            selected.append(row)
            brands.add(row["Бренд"])
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        for row in rows:
            if norm(row["Бренд"] + " — " + row["Название"]) in exclude_names:
                continue
            if row not in selected:
                selected.append(row)
            if len(selected) >= limit:
                break
    return selected[:limit]

def card(row, index):
    return (f"<b>{index}. {html.escape(row['Бренд'])}</b> — "
            f"{html.escape(row['Название'])}\n"
            f"<i>{html.escape(row.get('Категория',''))} · "
            f"{html.escape(row.get('Крепость',''))}</i>")

def build_result(profile, rows, memory, pairing_text=""):
    bowl = profile.get("bowl") or memory.get("usual_bowl") or "Классическая чаша"
    strength = profile.get("strength") or memory.get("usual_strength") or "Средняя"
    lines = ["<b>Вот что я бы сделал сегодня:</b>", ""]
    for i, row in enumerate(rows, 1):
        lines += [card(row, i), ""]
    lines += [
        f"🔥 <b>Чаша:</b> {html.escape(str(bowl))}",
        f"💪 <b>Крепость:</b> {html.escape(str(strength))}",
    ]
    blocked = merge_unique(profile["excluded_terms"], profile["allergies"])
    if blocked:
        lines.append("🚫 <b>Не использую:</b> " +
                     ", ".join(html.escape(x) for x in blocked))
    if pairing_text:
        lines += ["", pairing_text]
    return "\n".join(lines)

def local_profile_from_text(text):
    t = norm(text)
    p = empty_profile()
    p["desired_terms"] = [
        x for x in ["ягоды", "тропики", "сладкое", "кислое", "свежее", "цитрус"]
        if x in t
    ]
    for match in re.finditer(
        r"(?:не хочу|не люблю|без|не надо|аллергия на)\s+([^.!?\n]+)", t
    ):
        for token in re.split(r"[,;]+|\s+и\s+", match.group(1)):
            token = token.strip()
            if len(token) > 2 and token not in {"табак","вкус","вкусы","кальян"}:
                p["excluded_terms"].append(canonical_term(token))
    if "класс" in t:
        p["bowl"] = "Классическая чаша"
    if "покреп" in t or "крепк" in t:
        p["strength"] = "Крепкие"
    elif "легк" in t or "мягк" in t:
        p["strength"] = "Легкие"
    elif "сред" in t:
        p["strength"] = "Средние"
    if "не притор" in t or "не слишком слад" in t:
        p["sweetness"] = "не слишком сладкое"
    if "без мяты" in t or "не люблю мят" in t or "без холод" in t:
        p["freshness"] = "без свежести/холода"
        p["excluded_terms"].append("мята")
    return p

def keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Подобрать", callback_data="new"),
         InlineKeyboardButton(text="🧠 Мой профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🔄 Ещё вариант", callback_data="again"),
         InlineKeyboardButton(text="🧹 Сбросить", callback_data="forget")]
    ])

def profile_text(memory):
    return (
        f"<b>🧠 Профиль {html.escape(memory.get('name') or 'гостя')}</b>\n\n"
        f"Любит: {html.escape(', '.join(memory.get('likes', [])[:8]) or 'пока не знаю')}\n"
        f"Не любит: {html.escape(', '.join(memory.get('dislikes', [])[:8]) or 'пока не знаю')}\n"
        f"Крепость: {html.escape(str(memory.get('usual_strength') or 'не знаю'))}\n"
        f"Чаша: {html.escape(str(memory.get('usual_bowl') or 'не знаю'))}\n"
        f"Последнее: {html.escape(', '.join(memory.get('last_hookahs', [])[:3]) or 'пока нет')}"
    )

async def transcribe_voice(bot, message):
    global _whisper_model
    tg_file = await bot.get_file(message.voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        path = tmp.name
    try:
        await bot.download(tg_file, destination=path)
        if _whisper_model is None:
            from faster_whisper import WhisperModel
            _whisper_model = await asyncio.to_thread(
                WhisperModel, WHISPER_MODEL, device="cpu", compute_type="int8"
            )
        segments, _ = await asyncio.to_thread(
            _whisper_model.transcribe,
            path,
            language="ru",
            vad_filter=True,
            beam_size=1,
            condition_on_previous_text=False,
            without_timestamps=True
        )
        return " ".join(s.text.strip() for s in segments).strip()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

async def handle_turn(bot, message, user_text):
    session = sessions.setdefault(message.from_user.id, {
        "profile": profile_from_memory(load_memory(message.from_user.id, message)),
        "history": [],
        "memory": load_memory(message.from_user.id, message),
        "turns": 0, "shown": [], "counted": False
    })
    session["turns"] += 1

    ai = await ai_understand(session, user_text)
    if ai:
        session["profile"] = merge_profile(session["profile"], ai)
    else:
        session["profile"] = merge_profile(
            session["profile"], local_profile_from_text(user_text)
        )

    prefs = session["profile"]

    # Сохраняем предпочтения сразу после сообщения, даже если подбор ещё не нужен.
    update_memory(
        session["memory"], ai or {}, prefs,
        recommendations=None, increment_visit=False
    )

    blocked = prefs["excluded_terms"] + prefs["allergies"]
    has_positive = bool(
        prefs["desired_terms"] or prefs["desired_categories"] or
        prefs.get("strength") or prefs.get("bowl")
    )

    # Сообщение только о запрете/нежелании: фиксируем и продолжаем короткий диалог.
    if blocked and not has_positive:
        reply = "Запомнил. Не буду предлагать: " + ", ".join(blocked[-5:]) + "."
        await message.answer(html.escape(reply))
        session["history"] += [
            {"role": "user", "text": user_text},
            {"role": "assistant", "text": reply}
        ]
        return

    if not has_positive:
        question = str((ai or {}).get("clarifying_question") or "").strip()
        if not question:
            question = "Что любишь больше: ягоды, фрукты, цитрус или что-то необычное?"
        await message.answer(html.escape(question[:180]))
        session["history"] += [
            {"role": "user", "text": user_text},
            {"role": "assistant", "text": question[:180]}
        ]
        return

    rows = score_candidates(prefs, session["memory"])
    recs = make_recommendations(rows, exclude_names=session.get("shown", []))
    if not recs:
        session["shown"] = []
        recs = make_recommendations(rows)

    if not recs:
        await message.answer(
            "Не нашёл вариант, который одновременно подходит и не нарушает твои запреты. "
            "Скажи, чем можно заменить один из запросов.",
            reply_markup=keyboard()
        )
        return

    if not session.get("counted"):
        update_memory(
            session["memory"], ai or {}, prefs,
            recommendations=recs, increment_visit=True
        )
        session["counted"] = True

    session["shown"] = merge_unique(
        session.get("shown", []),
        [r["Бренд"] + " — " + r["Название"] for r in recs]
    )
    session["history"] += [
        {"role": "user", "text": user_text},
        {"role": "assistant", "text": "recommendations"}
    ]

    reply = str((ai or {}).get("reply") or "").strip()
    wants_mix = any(
        x in norm(user_text)
        for x in ("микс", "сочетание", "рецепт", "пропорци")
    )
    pairing_text = build_pairing_text(prefs, ASSORTMENT) if wants_mix else ""
    result_text = build_result(prefs, recs, session["memory"], pairing_text)
    if reply:
        result_text = html.escape(reply[:220]) + "\n\n" + result_text

    await message.answer(result_text, reply_markup=keyboard())

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    # Загружаем Whisper при старте, чтобы первое голосовое не ждало загрузку модели.
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = await asyncio.to_thread(
            WhisperModel, WHISPER_MODEL, device="cpu", compute_type="int8"
        )

    @dp.message(CommandStart())
    async def start(message: Message):
        sessions[message.from_user.id] = {
            "profile": profile_from_memory(load_memory(message.from_user.id, message)), "history": [],
            "memory": load_memory(message.from_user.id, message), "turns": 0, "shown": [], "counted": False
        }
        name = html.escape(message.from_user.first_name or "гость")
        await message.answer(
            f"<b>Привет, {name} 👋</b>\n\n"
            "Я запомню твой вкус и со временем буду подбирать точнее.\n"
            "Скажи просто: чего хочется сегодня?",
            reply_markup=keyboard()
        )

    @dp.callback_query(F.data == "new")
    async def new_chat(call: CallbackQuery):
        sessions[call.from_user.id] = {
            "profile": profile_from_memory(load_memory(call.from_user.id, call.message)), "history": [],
            "memory": load_memory(call.from_user.id, call.message), "turns": 0, "shown": [], "counted": False
        }
        await call.answer()
        await call.message.answer("Окей. Что хочется сегодня? Можно голосом.")

    @dp.callback_query(F.data == "profile")
    async def profile(call: CallbackQuery):
        memory = load_memory(call.from_user.id, call.message)
        await call.answer()
        await call.message.answer(profile_text(memory), reply_markup=keyboard())

    @dp.callback_query(F.data == "forget")
    async def forget(call: CallbackQuery):
        memory = empty_memory(call.from_user.id, call.message)
        save_memory(memory)
        sessions[call.from_user.id] = {
            "profile": empty_profile(), "history": [],
            "memory": memory, "turns": 0, "shown": [], "counted": False
        }
        await call.answer("Память сброшена")
        await call.message.answer("Готово. Начинаем с чистого листа.", reply_markup=keyboard())

    @dp.callback_query(F.data == "again")
    async def again(call: CallbackQuery):
        session = sessions.get(call.from_user.id)
        if not session:
            session = {
                "profile": empty_profile(), "history": [],
                "memory": load_memory(call.from_user.id, call.message),
                "turns": 0, "shown": [], "counted": False
            }
        rows = score_candidates(session["profile"], session["memory"])
        recs = make_recommendations(rows, exclude_names=session.get("shown", []))
        if not recs:
            session["shown"] = []
            recs = make_recommendations(rows)
        session["shown"] = merge_unique(session.get("shown"), [r["Бренд"] + " — " + r["Название"] for r in recs])
        await call.answer()
        if recs:
            pairing_text = build_pairing_text(session["profile"], ASSORTMENT)
            await call.message.answer(build_result(session["profile"], recs, session["memory"], pairing_text),
                                      reply_markup=keyboard())
        else:
            await call.message.answer("Дай ещё одно пожелание — и я докручу подбор.",
                                      reply_markup=keyboard())

    @dp.message(F.voice)
    async def voice(message: Message):
        text = await transcribe_voice(bot, message)
        if text:
            await handle_turn(bot, message, text)
        else:
            await message.answer("Не разобрал голосовое. Попробуй ещё раз или напиши текстом.")

    @dp.message(F.text)
    async def text_message(message: Message):
        if message.text and message.text.strip():
            await handle_turn(bot, message, message.text.strip())

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
