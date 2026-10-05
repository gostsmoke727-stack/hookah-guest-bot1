import asyncio
import base64
import csv
import html
import json
import os
import re
import tempfile
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, BotCommand

BASE = Path(__file__).resolve().parent
CSV_PATH = BASE / "data" / "assortment.csv"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").replace("\ufeff", "").strip().strip("\"").strip("'").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as f:
    ASSORTMENT = [r for r in csv.DictReader(f) if r.get("Бренд") and r.get("Название")]

sessions = {}
_whisper_model = None

BOWL_PHOTOS = {
    "классическая": "https://smokestationchicago.com/cdn/shop/products/egyptian-clay-hookah-bowl-sku-634-16552480931978.jpg?v=1593616062",
    "phunnel": "https://upload.wikimedia.org/wikipedia/commons/d/dc/Shisha_Phunnel_Kopf.jpg",
}
BOWL_PHOTO_CREDITS = {
    "классическая": "Фото: Smoke Station — классическая египетская чаша.",
    "phunnel": "Фото: HookahFloW / Wikimedia Commons, CC BY-SA 4.0.",
}

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
Ты — AI-мастер кальяна премиального заведения. Веди живой разговор, а не анкету.

Правила:
- Явное "не хочу", "не люблю", "без", аллергия = абсолютный запрет.
- Не возвращай запрещённое в desired_terms.
- Не выдумывай наличие, бренды, вкусы или рецепты: каталог проверяется кодом.
- Новое явное пожелание важнее старого.
- "воздух", "детский", "максимально лёгкий" = 1/10.
- "лёгкий" = 2–3/10; "средний" = 4–6/10; "крепкий" = 7–9/10; "очень крепкий", "убойный" = 10/10.
- Если названо число 1–10, сохрани его в strength_level.
- "классика" = классическая чаша.
- "ягоды с тропиками" = две положительные оси одновременно.
- Рецепт показывай только при прямом запросе на микс/сочетание/рецепт/пропорции.
- Отвечай естественно, 1–3 предложения. Не задавай вопрос, если данных достаточно.

Верни ТОЛЬКО JSON:
{
 "reply": "естественная реплика гостю",
 "name": "",
 "desired_terms": [],
 "desired_categories": [],
 "excluded_terms": [],
 "allergies": [],
 "bowl": null,
 "strength": null,
 "strength_level": null,
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
        "allergies": [], "bowl": None, "strength": None, "strength_level": None,
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
        "favorite_flavors": [], "favorite_mixes": [], "last_hookahs": [],
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
    for key in ("bowl", "strength", "strength_level", "sweetness", "freshness", "frequency", "mood"):
        value = data.get(key)
        if value is not None and norm(value) not in {"null", "none"}:
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
    if profile.get("strength_level") is not None:
        memory["strength_level"] = int(profile["strength_level"])
    if profile.get("bowl"):
        memory["usual_bowl"] = profile["bowl"]
    if recommendations:
        recent = [r["Бренд"] + " — " + r["Название"] for r in recommendations[:3]]
        memory["last_hookahs"] = merge_unique(recent, memory.get("last_hookahs", []))[:12]
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
        "strength_level": memory.get("strength_level"),
    })

def parse_ai_json(text):
    text=str(text or "").strip()
    fence=chr(96)*3
    text=re.sub(r"^"+re.escape(fence)+r"(?:json)?\s*","",text,flags=re.I)
    text=re.sub(r"\s*"+re.escape(fence)+r"$","",text)
    try: return json.loads(text)
    except json.JSONDecodeError:
        match=re.search(r"\{.*\}",text,flags=re.S)
        return json.loads(match.group(0)) if match else None

def gemini_text_request(system_prompt,user_prompt):
    if not GEMINI_API_KEY: return None
    payload={"system_instruction":{"parts":[{"text":system_prompt}]},"contents":[{"role":"user","parts":[{"text":user_prompt}]}],"generationConfig":{"temperature":0.2,"maxOutputTokens":280}}
    url="https://generativelanguage.googleapis.com/v1beta/models/"+GEMINI_MODEL+":generateContent?key="+urllib.parse.quote(GEMINI_API_KEY)
    req=urllib.request.Request(url,data=json.dumps(payload,ensure_ascii=False).encode(),headers={"Content-Type":"application/json"},method="POST")
    try:
        with urllib.request.urlopen(req,timeout=25) as response: data=json.loads(response.read().decode())
        parts=data.get("candidates",[{}])[0].get("content",{}).get("parts",[])
        return parse_ai_json("".join(str(p.get("text","")) for p in parts if p.get("text")))
    except Exception: return None

def openrouter_request(system_prompt,user_prompt):
    if not OPENROUTER_API_KEY: return None
    payload={"model":OPENROUTER_MODEL,"messages":[{"role":"system","content":system_prompt},{"role":"user","content":user_prompt}],"temperature":0.2,"max_tokens":280}
    req=urllib.request.Request("https://openrouter.ai/api/v1/chat/completions",data=json.dumps(payload,ensure_ascii=False).encode(),headers={"Content-Type":"application/json","Authorization":"Bearer "+OPENROUTER_API_KEY,"HTTP-Referer":"https://github.com/gostsmoke727-stack/hookah-guest-bot1","X-Title":"Hookah Guest Bot"},method="POST")
    try:
        with urllib.request.urlopen(req,timeout=25) as response: data=json.loads(response.read().decode())
        return parse_ai_json(data["choices"][0]["message"]["content"])
    except Exception: return None

async def ai_understand(session, user_text):
    prompt = (
        "ПАМЯТЬ ГОСТЯ:\n" + json.dumps(session["memory"], ensure_ascii=False) +
        "\nТЕКУЩИЙ ПРОФИЛЬ:\n" + json.dumps(session["profile"], ensure_ascii=False) +
        "\nИСТОРИЯ:\n" + json.dumps(session["history"][-10:], ensure_ascii=False) +
        "\nСООБЩЕНИЕ ГОСТЯ:\n" + user_text
    )
    return await asyncio.to_thread(gemini_text_request, AI_SYSTEM, prompt) or await asyncio.to_thread(openrouter_request, AI_SYSTEM, prompt)

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

def strength_to_level(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return max(1, min(10, int(value)))
    t = norm(value)
    m = re.search(r"(?<!\d)(10|[1-9])(?!\d)", t)
    if m:
        return int(m.group(1))
    if any(x in t for x in ("воздух", "детский", "максимально лег")):
        return 1
    if "очень креп" in t or "убойн" in t or "максимально креп" in t:
        return 10
    if "креп" in t:
        return 8
    if "сред" in t:
        return 5
    if "лег" in t or "мяг" in t:
        return 3
    return None

def row_strength_level(row):
    return strength_to_level(row.get("Крепость", ""))

def recent_names(memory):
    return {norm(x) for x in (memory.get("last_hookahs") or [])}

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
        target_level = profile.get("strength_level") or strength_to_level(strength)
        if target_level:
            delta = abs(row_strength_level(row) - int(target_level))
            score += 10 if delta == 0 else 7 if delta == 1 else 4 if delta == 2 else 1 if delta <= 3 else -4
        if norm(row["Бренд"] + " — " + row["Название"]) in recent_names(memory):
            score -= 18

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


def recipe_strength_score(resolved, ratios):
    weights = {"легкие": 3, "легкая": 3, "средние": 5, "средняя": 5, "крепкие": 8, "крепкий": 8, "крепкая": 8}
    total = 0.0
    for row, ratio in zip(resolved, ratios):
        try:
            pct = float(str(ratio).replace("%", "").replace(",", "."))
        except ValueError:
            pct = 0
        total += weights.get(norm(row.get("Крепость", "")), 5) * pct / 100
    return max(1, min(10, round(total)))

def find_substitute(row, target_level=None):
    direction = norm(row.get("Направление", ""))
    category = norm(row.get("Категория", ""))
    target = target_level or row_strength_level(row) or 5
    pool = []
    for candidate in ASSORTMENT:
        if candidate["Бренд"] == row["Бренд"] and candidate["Название"] == row["Название"]:
            continue
        same_family = norm(candidate.get("Направление", "")) == direction or norm(candidate.get("Категория", "")) == category
        if not same_family:
            continue
        pool.append((abs((row_strength_level(candidate) or 5) - target), candidate))
    pool.sort(key=lambda x: (x[0], norm(x[1]["Название"])))
    return pool[0][1] if pool else None

def resolve_pairing_for_rows(rows):
    names = {(norm(r["Бренд"]), norm(r["Название"])) for r in rows}
    for pairing in CURATED_PAIRINGS:
        resolved = []
        ok = True
        for term in pairing["terms"]:
            found = [r for r in ASSORTMENT if term_matches(r, term) and (norm(r["Бренд"]), norm(r["Название"])) in names]
            if not found:
                ok = False
                break
            resolved.append(found[0])
        if ok:
            return pairing, resolved
    return None

def ratio_numbers(ratios):
    out=[]
    for ratio in ratios:
        m=re.search(r"(\d+(?:[.,]\d+)?)", str(ratio))
        out.append(float(m.group(1).replace(",", ".")) if m else 0.0)
    total=sum(out)
    return [(x/total*100 if total else 0) for x in out]

def bowl_kind(profile=None):
    value = norm((profile or {}).get("bowl"))
    if any(x in value for x in ("класс", "егип", "турец", "традиц")):
        return "классическая"
    return "phunnel"

def normalized_bowl(profile):
    t=norm(profile.get("bowl") or "")
    if any(x in t for x in ("класс","егип","турец","традиц")): return "Классическая"
    if "vortex" in t or "ворте" in t: return "Vortex"
    if "phunnel" in t or "фанн" in t: return "Phunnel"
    return "Phunnel"

def bowl_technique(rows,profile=None):
    max_level=max([row_strength_level(r) or 5 for r in rows] or [5])
    bowl=normalized_bowl(profile or {})
    if bowl=="Классическая": pack,grams="рыхлая/полуплотная; не перекрывать отверстия","15–20 г"
    elif max_level>=8: pack,grams="полуплотная/плотная под конкретный табак","18–22 г"
    else: pack,grams="рыхлая/полуплотная","15–20 г"
    return f"<b>🥣 Чаша:</b> {bowl}, {grams}; {pack}. Отступ около 2–3 мм."

def bowl_photo_query(profile):
    return {"Классическая":"traditional Egyptian hookah bowl shisha","Vortex":"vortex hookah bowl shisha","Phunnel":"phunnel hookah bowl shisha"}.get(normalized_bowl(profile),"phunnel hookah bowl shisha")

def detailed_pairing_text(profile, rows):
    result=resolve_pairing_for_rows(rows)
    if not result:
        if len(rows)==1:
            row=rows[0]
            lvl=row_strength_level(row) or 5
            return (
                "<b>🧾 Подробный состав</b>\\n\\n"
                f"<b>{html.escape(row['Бренд'])} — {html.escape(row['Название'])}</b>\\n"
                "100% одного вкуса.\\n\\n"
                f"<b>Крепость:</b> {strength_label(lvl)}.\\n"
                f"{bowl_technique([row])}\\n"
                "<b>👐 Забивка:</b> разрыхлить, распределить равномерно, не утрамбовывать без необходимости.\\n"
                "<b>🔥 Жар:</b> старт умеренный; горчит — уменьшить жар, вкус плоский — постепенно добавить.\\n\\n"
                "<i>Подтверждённой рецептуры микса нет — пропорции не выдумываю.</i>"
            )
        return "<b>🧾 Подробный состав</b>\\n\\nДля этого сочетания нет подтверждённого рецепта с пропорциями. Я не буду выдавать авторский расчёт за опубликованный рецепт."

    pairing,resolved=result
    ratios=pairing["ratio"]
    percentages=ratio_numbers(ratios)
    level=recipe_strength_score(resolved,ratios)
    lines=[
        "<b>🧾 Подробный состав</b>","",
        f"<b>{html.escape(pairing['name'])}</b>",
        f"Источник: {html.escape(pairing['source'])}","",
        "<b>📐 Рецепт</b>"
    ]
    for row,pct in zip(resolved,percentages):
        g15=pct*15/100; g20=pct*20/100; g25=pct*25/100
        lines.append(
            f"• <b>{pct:.0f}%</b> — {html.escape(row['Бренд'])} — {html.escape(row['Название'])} "
            f"({g15:.1f} г / {g20:.1f} г / {g25:.1f} г для чаши 15/20/25 г)"
        )
    lines += [
        "",
        f"<b>💪 Крепость микса:</b> {level}/10 — {strength_label(level).split('—',1)[-1].strip()}.",
        "Это расчётная шкала по компонентам и долям, а не лабораторное измерение никотина.",
        "",
        bowl_technique(resolved, profile),
        "<b>👐 Забивка:</b> разрыхлить каждый компонент, смешать и равномерно распределить; отверстия не закрывать.",
        "<b>🔥 Старт:</b> умеренный жар. Резкость/гарь — убавить; плоский вкус — постепенно добавить.",
        "",
        "<b>🪶 Сделать легче:</b> уменьшить долю самого крепкого компонента и заменить её близким по вкусовой роли. Для 1–3/10 не пытаться решить вопрос только жаром.",
        "",
        "<b>💪 Сделать крепче:</b> постепенно увеличить долю крепкого компонента или заменить часть мягкой основы. Для 8–10/10 учитывать реальную крепость табака.",
        "",
        "<b>🔁 Если компонента нет:</b>"
    ]
    for row in resolved:
        sub=find_substitute(row)
        if sub:
            lines.append(f"• {html.escape(row['Название'])} → {html.escape(sub['Бренд'])} — {html.escape(sub['Название'])}.")
    lines += [
        "",
        "<b>Важно:</b> ощущение крепости зависит также от бренда, листа, температуры, чаши и длительности сессии."
    ]
    return "\\n".join(lines)

def favorite_mix_from_rows(rows, source=""):
    return {
        "name": " + ".join(r["Бренд"] + " — " + r["Название"] for r in rows),
        "components": [{"brand": r["Бренд"], "name": r["Название"], "strength": r.get("Крепость", ""), "direction": r.get("Направление", ""), "category": r.get("Категория", "")} for r in rows],
        "source": source, "saved_at": now_iso()
    }

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

def make_recommendations(rows, limit=3, exclude_names=None, diversity_offset=0):
    exclude_names = {norm(x) for x in (exclude_names or [])}
    pool = [r for r in rows if norm(r["Бренд"] + " — " + r["Название"]) not in exclude_names]
    if not pool:
        return []
    start = diversity_offset % len(pool)
    rotated = pool[start:] + pool[:start]
    selected, brands = [], set()
    for row in rotated:
        if row["Бренд"] not in brands:
            selected.append(row)
            brands.add(row["Бренд"])
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        for row in rotated:
            if row not in selected:
                selected.append(row)
            if len(selected) >= limit:
                break
    return selected[:limit]


def card(row, index):
    return (
        f"<b>{index}. {html.escape(row['Бренд'])} — {html.escape(row['Название'])}</b>\n"
        f"Категория: {html.escape(row.get('Категория',''))}\n"
        f"Направление: {html.escape(row.get('Направление',''))}\n"
        f"Крепость табака: {html.escape(row.get('Крепость',''))}"
    )

def strength_label(level):
    level = strength_to_level(level)
    if level is None:
        return "не задана"
    if level == 1:
        return "1/10 — воздух / детский"
    if level == 2:
        return "2/10 — очень лёгкая"
    if level == 3:
        return "3/10 — лёгкая"
    if level <= 6:
        return f"{level}/10 — средняя"
    if level <= 9:
        return f"{level}/10 — крепкая"
    return "10/10 — очень крепкая"

def build_result(profile, rows, memory, pairing_text=""):
    bowl = profile.get("bowl") or memory.get("usual_bowl") or "Классическая чаша"
    level = profile.get("strength_level") or memory.get("strength_level")
    strength = strength_label(level or profile.get("strength") or memory.get("usual_strength") or 5)
    lines = ["<b>🔥 Подбор</b>", ""]
    for i, row in enumerate(rows, 1):
        lines.append(f"<b>{i}. {html.escape(row['Бренд'])} — {html.escape(row['Название'])}</b> · {html.escape(row.get('Направление',''))} · {html.escape(row.get('Крепость',''))}")
    lines += ["", f"🥣 {html.escape(str(bowl))}", f"💪 {html.escape(str(strength))}"]
    blocked = merge_unique(profile["excluded_terms"], profile["allergies"])
    if blocked:
        lines.append("🚫 Не предлагаю: " + html.escape(", ".join(blocked[-6:])))
    if pairing_text:
        lines += ["", pairing_text]
    return "\n".join(lines)


def local_profile_from_text(text):
    t = norm(text)
    p = empty_profile()
    p["desired_terms"] = [x for x in ["ягоды", "тропики", "сладкое", "кислое", "свежее", "цитрус"] if x in t]
    for match in re.finditer(r"(?:не хочу|не люблю|без|не надо|аллергия на)\s+([^.!?\n]+)", t):
        for token in re.split(r"[,;]+|\s+и\s+", match.group(1)):
            token = token.strip()
            if len(token) > 2 and token not in {"табак","вкус","вкусы","кальян"}:
                p["excluded_terms"].append(canonical_term(token))
    if "класс" in t:
        p["bowl"] = "Классическая чаша"
    level = strength_to_level(t)
    if level is not None:
        p["strength_level"] = level
        p["strength"] = "Легкие" if level <= 3 else ("Средние" if level <= 6 else "Крепкие")
    if "не притор" in t or "не слишком слад" in t:
        p["sweetness"] = "не слишком сладкое"
    if "без мяты" in t or "не люблю мят" in t or "без холод" in t:
        p["freshness"] = "без свежести/холода"
        p["excluded_terms"].append("мята")
    return p



def recipe_for_selection(rows, profile):
    if not rows:
        return None
    curated = resolve_pairing_for_rows(rows)
    if curated:
        pairing, resolved = curated
        return {"name": pairing["name"], "rows": resolved, "ratios": pairing["ratio"], "verified": True, "source": pairing["source"]}
    selected = rows[:3]
    ratios = ["100%"] if len(selected) == 1 else (["60%", "40%"] if len(selected) == 2 else ["50%", "30%", "20%"])
    return {"name": "Авторский подбор", "rows": selected, "ratios": ratios, "verified": False, "source": "расчёт бота по текущему ассортименту"}

def weighted_strength(rows, ratios):
    vals = []
    for row, ratio in zip(rows, ratios):
        level = row_strength_level(row)
        if level is not None:
            vals.append((level, int(str(ratio).replace("%", ""))))
    if not vals:
        return None
    total = sum(p for _, p in vals)
    return round(sum(level * p for level, p in vals) / total, 1)

def detailed_recipe_text(rows, profile):
    recipe = recipe_for_selection(rows, profile)
    if not recipe:
        return "Не удалось определить состав."
    lines = [f"<b>🧾 Готовое сочетание — {html.escape(recipe['name'])}</b>", ""]
    if recipe["verified"]:
        lines.append(f"✅ <b>Проверенный рецепт:</b> {html.escape(recipe['source'])}")
    else:
        lines.append("🧪 <b>Авторский расчёт:</b> пропорции не выдаются за опубликованный рецепт.")
    lines += ["", "<b>⚖️ Что забивать</b>"]
    for row, ratio in zip(recipe["rows"], recipe["ratios"]):
        pct = ratio_numbers([ratio])[0]
        grams = round(20 * pct / 100, 1)
        lines.append(f"• <b>{ratio}</b> — {html.escape(row['Бренд'])} — {html.escape(row['Название'])} → <b>{grams:g} г</b> на чашу 20 г")
    mix_strength = weighted_strength(recipe["rows"], recipe["ratios"])
    lines += ["", f"<b>💪 Крепость:</b> {mix_strength}/10" if mix_strength is not None else "<b>💪 Крепость:</b> не удалось рассчитать", bowl_technique(recipe["rows"], profile), "<b>🔥 Жар:</b> старт умеренный; если жёстко/горчит — снижай, если плоско — добавляй постепенно.", "", "<b>🔽 Легче:</b> уменьши долю самого крепкого компонента и замени её более лёгким аналогом.", "<b>🔼 Крепче:</b> увеличь долю крепкого компонента или замени часть основы на более крепкий табак.", "", "<b>🔁 Замены:</b>"]
    for row in recipe["rows"]:
        sub = find_substitute(row, row_strength_level(row))
        if sub:
            lines.append(f"• {html.escape(row['Название'])} → {html.escape(sub['Бренд'])} — {html.escape(sub['Название'])}")
    lines += ["", "<b>📚 Теория</b>", "Крепость — расчётная шкала по компонентам и долям, а не точное измерение никотина.", "Чаша, плотность забивки, влажность табака и жар влияют на раскрытие вкуса и субъективную крепость.", "Если рецепт опубликован, пропорции сохранены без самовольной замены. Если компонент запрещён гостем или отсутствует, рецепт считается неподходящим."]
    return "\n".join(lines)

def remember_current_mix(session):
    rows = session.get("last_mix_rows") or []
    recipe = recipe_for_selection(rows, session.get("profile") or empty_profile())
    if not recipe:
        return None
    mix = {
        "name": recipe["name"],
        "rows": [{"brand": r["Бренд"], "name": r["Название"], "ratio": ratio} for r, ratio in zip(recipe["rows"], recipe["ratios"])],
        "verified": recipe["verified"],
        "source": recipe["source"],
        "saved_at": now_iso(),
    }
    mixes = list(session["memory"].get("favorite_mixes") or [])
    session["memory"]["favorite_mixes"] = [mix] + mixes[:9]
    save_memory(session["memory"])
    return mix

def commands_text():
    return (
        "<b>📚 Команды бота</b>\n\n"
        "<b>💬 Общение</b>\nПросто напиши или отправь голосовое — бот поймёт пожелания.\n\n"
        "<b>🧠 Память</b>\n• <code>запомни</code> — сохранить понравившееся сочетание\n• <code>мой профиль</code> — показать твой вкус\n\n"
        "<b>🔥 Подбор</b>\n• <code>ещё вариант</code> — другой вариант без повтора\n• <code>подробный состав</code> — рецепт и технология выбранного сочетания\n\n"
        "<b>⚙️ Сервис</b>\n• <code>меню</code> — открыть меню команд\n• <code>сбросить</code> — начать с чистого листа"
    )

def menu_keyboard(show_mix=False):
    rows = [
        [InlineKeyboardButton(text="🔥 Подобрать", callback_data="new"),
         InlineKeyboardButton(text="📋 Подробный состав", callback_data="details")],
    ]
    if show_mix:
        rows.append([InlineKeyboardButton(text="🎯 Готовое сочетание", callback_data="mix")])
    rows += [
        [InlineKeyboardButton(text="🧠 Мой профиль", callback_data="profile"), InlineKeyboardButton(text="💾 Запомнить", callback_data="remember")],
        [InlineKeyboardButton(text="🔄 Ещё вариант", callback_data="again"), InlineKeyboardButton(text="🧹 Сбросить", callback_data="forget")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def keyboard(rows=None):
    buttons=[[InlineKeyboardButton(text="🔥 Подобрать",callback_data="new"),InlineKeyboardButton(text="📋 Все команды",callback_data="menu")]]
    if rows:
        for i,_ in enumerate(rows[:3]): buttons.append([InlineKeyboardButton(text=f"🥣 Выбрать №{i+1}",callback_data=f"pick:{i}")])
        if len(rows)>=2: buttons.append([InlineKeyboardButton(text="🎯 Собрать сочетание",callback_data="mix")])
    buttons += [[InlineKeyboardButton(text="🧾 Подробный состав",callback_data="details"),InlineKeyboardButton(text="💾 Запомнить",callback_data="remember")],[InlineKeyboardButton(text="🔄 Ещё вариант",callback_data="again"),InlineKeyboardButton(text="🧠 Профиль",callback_data="profile")],[InlineKeyboardButton(text="🧹 Сбросить память",callback_data="forget")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def profile_text(memory):
    return (
        f"<b>🧠 Профиль {html.escape(memory.get('name') or 'гостя')}</b>\n\n"
        f"Любит: {html.escape(', '.join(memory.get('likes', [])[:8]) or 'пока не знаю')}\n"
        f"Не любит: {html.escape(', '.join(memory.get('dislikes', [])[:8]) or 'пока не знаю')}\n"
        f"Крепость: {html.escape(str(memory.get('usual_strength') or 'не знаю'))} / {html.escape(str(memory.get('strength_level') or '—'))}/10\n"
        f"Чаша: {html.escape(str(memory.get('usual_bowl') or 'не знаю'))}\n"
        f"Последнее: {html.escape(', '.join(memory.get('last_hookahs', [])[:3]) or 'пока нет')}"
    )

def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        model_name = os.getenv("WHISPER_MODEL", "base")
        _whisper_model = WhisperModel(model_name, device="cpu", compute_type="int8")
    return _whisper_model

def _transcribe_local(path):
    model = _get_whisper_model()
    segments, _ = model.transcribe(path, language="ru", beam_size=1, best_of=1, temperature=0, condition_on_previous_text=False, vad_filter=True, without_timestamps=True)
    return " ".join(s.text.strip() for s in segments if s.text.strip()).strip()

async def transcribe_voice(bot, message):
    tg_file = await bot.get_file(message.voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        path = tmp.name
    try:
        await bot.download(tg_file, destination=path)
        return await asyncio.to_thread(_transcribe_local, path)
    except Exception:
        return ""
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
        "turns": 0, "shown": [], "last_recs": [], "last_mix_rows": [], "counted": False
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
        prefs.get("strength") or prefs.get("strength_level") or prefs.get("bowl")
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
    recs = make_recommendations(rows, exclude_names=session.get("shown", []) + list(recent_names(session["memory"])), diversity_offset=session["turns"] * 3)
    if not recs:
        recs = make_recommendations(rows, exclude_names=session.get("shown", []), diversity_offset=session["turns"] * 3)
    if not recs:
        recs = make_recommendations(rows, diversity_offset=session["turns"] * 3)

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
    session["last_mix_rows"] = recs
    session["last_recs"] = recs
    session["history"] += [
        {"role": "user", "text": user_text},
        {"role": "assistant", "text": "recommendations"}
    ]

    reply = str((ai or {}).get("reply") or "").strip()
    wants_mix = any(x in norm(user_text) for x in ("микс", "сочетание", "рецепт", "пропорци"))
    pairing = find_curated_pairing(prefs, ASSORTMENT) if wants_mix else None

    if wants_mix:
        if pairing:
            session["last_mix_rows"] = pairing[1]
            if reply:
                await message.answer(html.escape(reply[:260]))
            await send_bowl_photo(message, prefs)
            await message.answer(
                build_pairing_text(prefs, pairing[1]) + "\n\nНажми «📋 Подробный состав» — там граммы, забивка, жар, замены и теория.",
                reply_markup=menu_keyboard(),
            )
            return
        session["last_mix_rows"] = recs
        if reply:
            await message.answer(html.escape(reply[:260]))
        await message.answer(
            "Не нашёл опубликованного сочетания, которое одновременно соблюдает все твои условия и есть в текущем ассортименте. Пропорции выдумывать не буду.",
            reply_markup=menu_keyboard(),
        )
        return

    session["last_mix_rows"] = recs
    show_mix = bool(find_curated_pairing(prefs, recs))
    result_text = (html.escape(reply[:260]) + "\n\n" if reply else "") + build_result(prefs, recs, session["memory"])
    await message.answer(result_text, reply_markup=menu_keyboard(show_mix=show_mix))



async def show_menu(message):
    await message.answer(
        "<b>📋 Команды бота</b>\\n\\n"
        "<b>🔥 Подобрать</b> — новый подбор.\\n"
        "<b>🔄 Ещё вариант</b> — другой вариант без повторения недавних.\\n"
        "<b>💾 Запомни</b> — сохранить последнее понравившееся сочетание.\\n"
        "<b>🧾 Подробный состав</b> — рецепт, пропорции, расчётная крепость, чаша, забивка, жар и замены.\\n"
        "<b>🧠 Мой профиль</b> — что бот запомнил.\\n"
        "<b>🧹 Сбросить память</b> — очистить предпочтения.\\n\\n"
        "Можно писать обычным языком или отправлять голосовые."
    )

async def remember_last_mix(message):
    session = sessions.get(message.from_user.id)
    if not session or not session.get("last_recs"):
        await message.answer("Сначала сделай подбор и выбери понравившийся вариант.", reply_markup=keyboard())
        return
    recs = session.get("last_mix_rows") or session["last_recs"]
    pairing = resolve_pairing_for_rows(recs)
    mix = favorite_mix_from_rows(recs, pairing[0]["source"] if pairing else "подбор бота")
    memory = session["memory"]
    favorites = list(memory.get("favorite_mixes") or [])
    key = {norm(x["brand"]+" — "+x["name"]) for x in mix["components"]}
    favorites = [x for x in favorites if {norm(y.get("brand","")+" — "+y.get("name","")) for y in x.get("components",[])} != key]
    memory["favorite_mixes"] = (favorites + [mix])[-20:]
    save_memory(memory)
    await message.answer("💾 <b>Запомнил.</b>\\n\\n" + html.escape(mix["name"]), reply_markup=keyboard())

async def show_details(message):
    session = sessions.get(message.from_user.id)
    if not session or not session.get("last_recs"):
        await message.answer("Сначала сделай подбор — тогда я смогу раскрыть конкретный состав.", reply_markup=keyboard())
        return
    rows = session.get("last_mix_rows") or session["last_recs"]
    if not session.get("last_mix_rows"):
        candidate = find_curated_pairing(session["profile"], ASSORTMENT)
        if candidate:
            rows = candidate[1]
    await message.answer(detailed_pairing_text(session["profile"], rows), reply_markup=keyboard())

async def show_favorites(message):
    memory = load_memory(message.from_user.id, message)
    mixes = memory.get("favorite_mixes") or []
    if not mixes:
        await message.answer("💾 Сохранённых сочетаний пока нет.", reply_markup=keyboard())
        return
    lines = ["<b>💾 Сохранённые сочетания</b>", ""]
    for i, mix in enumerate(reversed(mixes[-10:]), 1):
        lines.append(f"<b>{i}.</b> {html.escape(mix.get('name',''))}")
    await message.answer("\n".join(lines), reply_markup=keyboard())

async def find_bowl_photo(profile):
    query=bowl_photo_query(profile)
    url="https://commons.wikimedia.org/w/api.php?"+urllib.parse.urlencode({"action":"query","generator":"search","gsrsearch":query,"gsrnamespace":6,"gsrlimit":5,"prop":"imageinfo","iiprop":"url","iiurlwidth":900,"format":"json","origin":"*"})
    def fetch():
        req=urllib.request.Request(url,headers={"User-Agent":"HookahGuestBot/1.0"})
        with urllib.request.urlopen(req,timeout=8) as response: data=json.loads(response.read().decode())
        for page in list((data.get("query",{}).get("pages") or {}).values()):
            info=page.get("imageinfo") or []
            if info: return info[0].get("thumburl") or info[0].get("url")
        return None
    try: return await asyncio.to_thread(fetch)
    except Exception: return None

async def send_bowl_photo(bot,message,profile):
    photo_url=await find_bowl_photo(profile)
    if photo_url:
        try: await bot.send_photo(message.chat.id,photo_url,caption=f"🥣 {normalized_bowl(profile)} — пример чаши")
        except Exception: pass

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    await bot.set_my_commands([
        {"command": "start", "description": "Начать подбор"},
        {"command": "menu", "description": "Меню команд"},
        {"command": "remember", "description": "Запомнить сочетание"},
        {"command": "details", "description": "Подробный состав"},
        {"command": "profile", "description": "Мой профиль"},
        {"command": "again", "description": "Ещё вариант"},
        {"command": "reset", "description": "Сбросить память"},
    ])

    @dp.message(CommandStart())
    async def start(message: Message):
        memory = load_memory(message.from_user.id, message)
        sessions[message.from_user.id] = {
            "profile": profile_from_memory(memory),
            "history": [],
            "memory": memory,
            "turns": 0,
            "shown": [],
            "last_mix_rows": [],
            "counted": False,
        }
        name = html.escape(message.from_user.first_name or "гость")
        await message.answer(
            f"<b>Привет, {name} 👋</b>\n\n"
            "Я запомню твой вкус и со временем буду подбирать точнее.\n"
            "Скажи просто, чего хочется сегодня.",
            reply_markup=menu_keyboard(),
        )

    @dp.callback_query(F.data == "new")
    async def new_chat(call: CallbackQuery):
        memory = load_memory(call.from_user.id, call.message)
        sessions[call.from_user.id] = {
            "profile": profile_from_memory(memory),
            "history": [],
            "memory": memory,
            "turns": 0,
            "shown": [],
            "last_mix_rows": [],
            "counted": False,
        }
        await call.answer()
        await call.message.answer("Окей. Что хочется сегодня? Можно голосом.", reply_markup=menu_keyboard())

    @dp.callback_query(F.data.startswith("pick:"))
    async def pick_callback(call: CallbackQuery):
        session=sessions.get(call.from_user.id)
        if not session or not session.get("last_recs"):
            await call.answer("Сначала сделай подбор",show_alert=True); return
        try: index=int(call.data.split(":",1)[1])
        except (ValueError,IndexError):
            await call.answer("Не понял выбор",show_alert=True); return
        rows=session["last_recs"]
        if index<0 or index>=len(rows):
            await call.answer("Такого варианта нет",show_alert=True); return
        selected=[rows[index]]
        session["last_mix_rows"]=selected
        await call.answer("Выбрано 👍")
        await send_bowl_photo(call.bot,call.message,session["profile"])
        await call.message.answer(detailed_pairing_text(session["profile"],selected),reply_markup=keyboard(selected))

    @dp.callback_query(F.data == "mix")
    async def mix_callback(call: CallbackQuery):
        session=sessions.get(call.from_user.id)
        if not session or not session.get("last_recs"):
            await call.answer("Сначала сделай подбор",show_alert=True); return
        rows=session["last_recs"]
        pairing=find_curated_pairing(session["profile"],rows)
        session["last_mix_rows"]=pairing[1] if pairing else rows[:3]
        await call.answer("Сочетание готово 👍")
        await send_bowl_photo(call.bot,call.message,session["profile"])
        await call.message.answer(detailed_pairing_text(session["profile"],session["last_mix_rows"]),reply_markup=keyboard(session["last_mix_rows"]))

    @dp.callback_query(F.data == "profile")
    async def profile_callback(call: CallbackQuery):
        memory = load_memory(call.from_user.id, call.message)
        await call.answer()
        await call.message.answer(profile_text(memory), reply_markup=menu_keyboard())

    @dp.callback_query(F.data == "details")
    async def details_callback(call: CallbackQuery):
        session = sessions.get(call.from_user.id)
        if not session or not session.get("last_mix_rows"):
            await call.answer("Сначала сделай подбор", show_alert=True)
            return
        await call.answer()
        await send_bowl_photo(call.message, session["profile"])
        await call.message.answer(
            detailed_recipe_text(session["last_mix_rows"], session["profile"]),
            reply_markup=menu_keyboard(),
        )

    @dp.callback_query(F.data == "mix")
    async def mix_callback(call: CallbackQuery):
        await call.answer()
        session = sessions.get(call.from_user.id)
        if not session:
            return
        pairing = find_curated_pairing(session["profile"], session.get("last_recs") or session.get("last_mix_rows") or [])
        if not pairing:
            await call.message.answer("Для текущего запроса нет проверенного сочетания из доступного ассортимента. Я не буду придумывать пропорции.", reply_markup=menu_keyboard())
            return
        session["last_mix_rows"] = pairing[1]
        await send_bowl_photo(call.message, session["profile"])
        await call.message.answer(build_pairing_text(session["profile"], pairing[1]) + "\n\nНажми «📋 Подробный состав» — там граммы, забивка, жар, замены и теория.", reply_markup=menu_keyboard())

    @dp.callback_query(F.data == "remember")
    async def remember_callback(call: CallbackQuery):
        session = sessions.get(call.from_user.id)
        if not session or not session.get("last_mix_rows"):
            await call.answer("Сначала сделай подбор", show_alert=True)
            return
        mix = remember_current_mix(session)
        await call.answer("Запомнил 👍")
        await call.message.answer(
            "<b>🧠 Запомнил</b>\n"
            + html.escape(mix["name"])
            + "\n"
            + "\n".join(
                f"• {x['ratio']} {html.escape(x['brand'])} — {html.escape(x['name'])}"
                for x in mix["rows"]
            ),
            reply_markup=menu_keyboard(),
        )

    @dp.callback_query(F.data == "forget")
    async def forget_callback(call: CallbackQuery):
        memory = empty_memory(call.from_user.id, call.message)
        save_memory(memory)
        sessions[call.from_user.id] = {
            "profile": empty_profile(),
            "history": [],
            "memory": memory,
            "turns": 0,
            "shown": [],
            "last_mix_rows": [],
            "counted": False,
        }
        await call.answer("Память сброшена")
        await call.message.answer("Готово. Начинаем с чистого листа.", reply_markup=menu_keyboard())

    @dp.callback_query(F.data == "again")
    async def again_callback(call: CallbackQuery):
        session = sessions.get(call.from_user.id)
        if not session:
            memory = load_memory(call.from_user.id, call.message)
            session = {
                "profile": profile_from_memory(memory),
                "history": [],
                "memory": memory,
                "turns": 0,
                "shown": [],
                "last_mix_rows": [],
                "counted": False,
            }
            sessions[call.from_user.id] = session

        session["turns"] += 1
        rows = score_candidates(session["profile"], session["memory"])
        recs = make_recommendations(
            rows,
            exclude_names=session.get("shown", []) + list(recent_names(session["memory"])),
            diversity_offset=session["turns"] * 5,
        )
        if not recs:
            recs = make_recommendations(
                rows,
                exclude_names=session.get("shown", []),
                diversity_offset=session["turns"] * 5,
            )
        if not recs:
            recs = make_recommendations(rows, diversity_offset=session["turns"] * 5)

        if recs:
            session["last_mix_rows"] = recs
            session["shown"] = merge_unique(
                session.get("shown", []),
                [r["Бренд"] + " — " + r["Название"] for r in recs],
            )
            await call.answer()
            await call.message.answer(
                build_result(session["profile"], recs, session["memory"]),
                reply_markup=keyboard(recs),
            )
        else:
            await call.answer("Нужны ещё пожелания", show_alert=True)

    @dp.callback_query(F.data == "menu")
    async def menu_callback(call: CallbackQuery):
        await call.answer()
        await call.message.answer(commands_text(), reply_markup=menu_keyboard())

    @dp.message(F.voice)
    async def voice(message: Message):
        text = await transcribe_voice(bot, message)
        if text:
            await handle_turn(bot, message, text)
        else:
            await message.answer("Не разобрал голосовое. Попробуй ещё раз или напиши текстом.")

    @dp.message(F.text)
    async def text_message(message: Message):
        text = (message.text or "").strip()
        nt = norm(text).lstrip("/")

        if nt in {"меню", "menu", "команды", "команды бота"}:
            await message.answer(commands_text(), reply_markup=menu_keyboard())
            return

        if nt in {"запомни", "запомнить", "remember"}:
            session = sessions.get(message.from_user.id)
            if not session or not session.get("last_mix_rows"):
                await message.answer("Сначала сделай подбор, а потом напиши «запомни».")
                return
            mix = remember_current_mix(session)
            await message.answer(
                "<b>🧠 Запомнил</b>\n"
                + html.escape(mix["name"])
                + "\n"
                + "\n".join(
                    f"• {x['ratio']} {html.escape(x['brand'])} — {html.escape(x['name'])}"
                    for x in mix["rows"]
                ),
                reply_markup=menu_keyboard(),
            )
            return

        if nt in {"подробный состав", "подробно", "состав", "рецепт", "details"}:
            session = sessions.get(message.from_user.id)
            if not session or not session.get("last_mix_rows"):
                await message.answer("Сначала сделай подбор, а потом запроси подробный состав.")
                return
            await send_bowl_photo(message, session["profile"])
            await message.answer(
                detailed_recipe_text(session["last_mix_rows"], session["profile"]),
                reply_markup=menu_keyboard(),
            )
            return

        if nt in {"мой профиль", "профиль", "profile"}:
            memory = load_memory(message.from_user.id, message)
            await message.answer(profile_text(memory), reply_markup=menu_keyboard())
            return

        if nt in {"ещё вариант", "еще вариант", "again"}:
            await message.answer("Нажми «🔄 Ещё вариант» под последним подбором.", reply_markup=menu_keyboard())
            return

        if nt in {"сбросить", "reset", "забыть"}:
            memory = empty_memory(message.from_user.id, message)
            save_memory(memory)
            sessions[message.from_user.id] = {
                "profile": empty_profile(),
                "history": [],
                "memory": memory,
                "turns": 0,
                "shown": [],
                "last_mix_rows": [],
                "counted": False,
            }
            await message.answer("Готово. Начинаем с чистого листа.", reply_markup=menu_keyboard())
            return

        if text:
            await handle_turn(bot, message, text)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
