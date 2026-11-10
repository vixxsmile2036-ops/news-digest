"""Бот подписки на личную рассылку по региону.
  python bot.py --post-menu   # запостить в паблик сообщение с кнопками регионов
  python bot.py               # слушать бота постоянно (на сервере/ПК)
  python bot.py --once        # обработать накопившиеся нажатия и выйти (для GitHub Actions)
Сами новости рассылает digest.py по расписанию, бот только оформляет и отменяет подписки."""
import os, sys, time, yaml, requests
from datetime import datetime

import storage

TOKEN = os.environ["TG_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"
CFG = yaml.safe_load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"), encoding="utf-8"))
BOT = CFG["bot"]
REGIONS = BOT["regions"]


def tg(method, **params):
    params = {k: v for k, v in params.items() if v is not None}
    r = requests.post(f"{API}/{method}", json=params, timeout=70)
    r.raise_for_status()
    return r.json()["result"]


def menu_keyboard(url_buttons=False, username=None):
    rows = []
    for slug, r in REGIONS.items():
        text = f"📍 {r['name']}"
        if url_buttons:
            rows.append([{"text": text, "url": f"https://t.me/{username}?start={slug}"}])
        else:
            rows.append([{"text": text, "callback_data": f"r:{slug}"}])
    return {"inline_keyboard": rows}


def subscribe(state, chat, slug):
    old = state["users"].get(str(chat), {})
    state["users"][str(chat)] = {"region": slug, "since": old.get("since") or datetime.now().strftime("%Y-%m-%d")}
    tg("sendMessage", chat_id=chat, text=BOT["welcome_text"].format(region=REGIONS[slug]["name"]))
    return True


def handle(u, state):
    """Обрабатывает одно обновление. True, если список подписчиков изменился."""
    cq = u.get("callback_query")
    if cq:
        tg("answerCallbackQuery", callback_query_id=cq["id"])
        data, chat = cq.get("data", ""), cq["message"]["chat"]["id"]
        if data.startswith("r:") and data[2:] in REGIONS:
            return subscribe(state, chat, data[2:])
        return False
    msg = u.get("message") or {}
    if (msg.get("chat") or {}).get("type") != "private":
        return False
    text, chat = msg.get("text", ""), msg["chat"]["id"]
    if not text.startswith("/"):
        return False
    parts = text.split(maxsplit=1)
    cmd, arg = parts[0].split("@")[0], (parts[1].strip() if len(parts) > 1 else "")
    if cmd == "/start" and arg in REGIONS:
        return subscribe(state, chat, arg)
    if cmd in ("/start", "/regions"):
        cur = state["users"].get(str(chat))
        note = f"\nСейчас выбран: {REGIONS[cur['region']]['name']}" if cur and cur.get("region") in REGIONS else ""
        tg("sendMessage", chat_id=chat, text=BOT["choose_text"] + note, reply_markup=menu_keyboard())
        return False
    if cmd == "/stop":
        if state["users"].pop(str(chat), None) is not None:
            tg("sendMessage", chat_id=chat, text="Вы отписались от рассылки. Чтобы подписаться снова, нажмите /start")
            return True
        tg("sendMessage", chat_id=chat, text="Вы и не были подписаны. Выбрать регион: /start")
    return False


def poll(once=False):
    offset = None
    while True:
        try:
            state = storage.load()  # подписчиков мог изменить digest.py, поэтому читаем заново
            updates = tg("getUpdates", offset=offset, timeout=0 if once else 50,
                         allowed_updates=["message", "callback_query"])
        except Exception as e:
            print("getUpdates/load error", e)
            if once:
                raise
            time.sleep(5)
            continue
        changed = False
        for u in updates:
            offset = u["update_id"] + 1
            try:
                changed = handle(u, state) or changed
            except Exception as e:
                print("handle error", e)
        if changed:
            storage.save(state)  # сначала сохраняем, потом подтверждаем обновления
        if once:
            if updates:
                tg("getUpdates", offset=offset, timeout=0)
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
        if os.getenv("GITHUB_ACTIONS") and not storage.configured():
            sys.exit("Не заданы секреты GIST_ID и GIST_TOKEN: без них подписки не сохранятся")
        poll(once="--once" in sys.argv)
