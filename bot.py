"""Бот с выбором региона.
  python bot.py --post-menu   # запостить в паблик сообщение с кнопками регионов
  python bot.py               # слушать бота постоянно (на сервере/ПК)
  python bot.py --once        # обработать накопившиеся нажатия и выйти (для GitHub Actions)"""
import os, sys, time, html, yaml, requests
from datetime import datetime
from zoneinfo import ZoneInfo

import digest

TOKEN = os.environ["TG_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"
CFG = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "config.yaml"), encoding="utf-8"))
DEFAULTS, BOT = CFG.get("defaults", {}), CFG["bot"]
REGIONS = BOT["regions"]
CACHE_TTL = 3 * 3600   # одну и ту же подборку не пересобираем 3 часа
COOLDOWN = 20          # секунд между запросами одного человека
_cache, _last = {}, {}


def tg(method, **params):
    params = {k: v for k, v in params.items() if v is not None}
    r = requests.post(f"{API}/{method}", json=params, timeout=70)
    r.raise_for_status()
    return r.json()["result"]


def region_blocks(slug):
    reg = REGIONS[slug]
    d = {**DEFAULTS, "title": f"Новости: {reg['name']}", "show_date": True}
    for k in ("period_days", "news_per_topic", "style", "language", "max_items_per_topic"):
        if k in BOT:
            d[k] = BOT[k]
    topics = []
    for t in BOT["topics"]:
        feeds = list(t.get("feeds", [])) + (reg.get("feeds", []) if t.get("region_feeds") else [])
        topics.append({**t, "feeds": feeds,
                       "queries": [q.format(region=reg["name"], city=reg["city"]) for q in t["queries"]]})
    d["topics"] = topics
    now = datetime.now(ZoneInfo(DEFAULTS.get("timezone", "Asia/Yekaterinburg")))
    return digest.build_blocks(d, now)


def send_blocks(chat, blocks):
    messages, cur = [], ""
    for b in blocks:
        if len(cur) + len(b) + 2 > 3900 and cur:
            messages.append(cur.strip())
            cur = ""
        cur += b + "\n\n"
    if cur.strip():
        messages.append(cur.strip())
    for m in messages:
        tg("sendMessage", chat_id=chat, text=m, parse_mode="HTML", disable_web_page_preview=True)
        time.sleep(0.5)


def send_region(chat, slug):
    if time.time() - _last.get(chat, 0) < COOLDOWN:
        return tg("sendMessage", chat_id=chat, text="⏳ Подборка уже собирается, подождите минуту.")
    _last[chat] = time.time()
    tg("sendMessage", chat_id=chat, text=f"⏳ Собираю подборку: {REGIONS[slug]['name']}…")
    hit = _cache.get(slug)
    if hit and time.time() - hit[0] < CACHE_TTL:
        blocks = hit[1]
    else:
        try:
            blocks = region_blocks(slug)
        except Exception as e:
            print("region error", slug, e)
            blocks = []
        if blocks:
            _cache[slug] = (time.time(), blocks)
    if not blocks:
        return tg("sendMessage", chat_id=chat, text="Не получилось собрать подборку, попробуйте позже.")
    send_blocks(chat, blocks)


def menu_keyboard(url_buttons=False, username=None):
    rows = []
    for slug, r in REGIONS.items():
        text = f"📍 {r['name']}"
        if url_buttons:
            rows.append([{"text": text, "url": f"https://t.me/{username}?start={slug}"}])
        else:
            rows.append([{"text": text, "callback_data": f"r:{slug}"}])
    return {"inline_keyboard": rows}


def handle(u):
    cq = u.get("callback_query")
    if cq:
        tg("answerCallbackQuery", callback_query_id=cq["id"])
        data = cq.get("data", "")
        if data.startswith("r:") and data[2:] in REGIONS:
            send_region(cq["message"]["chat"]["id"], data[2:])
        return
    msg = u.get("message") or {}
    text, chat = msg.get("text", ""), (msg.get("chat") or {}).get("id")
    if not chat or not text.startswith("/"):
        return
    parts = text.split(maxsplit=1)
    cmd, arg = parts[0].split("@")[0], (parts[1].strip() if len(parts) > 1 else "")
    if cmd == "/start" and arg in REGIONS:
        send_region(chat, arg)
    elif cmd in ("/start", "/regions"):
        tg("sendMessage", chat_id=chat, text=BOT["menu_text"], reply_markup=menu_keyboard())


def poll(once=False):
    offset = None
    while True:
        try:
            updates = tg("getUpdates", offset=offset, timeout=0 if once else 50,
                         allowed_updates=["message", "callback_query"])
        except Exception as e:
            print("getUpdates error", e)
            if once:
                return
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                handle(u)
            except Exception as e:
                print("handle error", e)
        if once:
            if updates:
                tg("getUpdates", offset=offset, timeout=0)  # подтвердить, что обработали
            return


def post_menu():
    username = tg("getMe")["username"]
    chat = BOT.get("channel") or os.environ["TG_CHAT_ID"]
    tg("sendMessage", chat_id=chat, text=BOT["menu_text"],
       reply_markup=menu_keyboard(url_buttons=True, username=username))
    print("Меню опубликовано")


if __name__ == "__main__":
    if "--post-menu" in sys.argv:
        post_menu()
    else:
        poll(once="--once" in sys.argv)
