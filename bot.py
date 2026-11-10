"""Telegram-бот подписок и приватная админ-панель управления дайджестом."""
import os, sys, time, yaml, requests, subprocess
from datetime import datetime
import storage

ROOT = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ["TG_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"
BASE_CFG = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
ADMIN_IDS = {x.strip() for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if x.strip()}


def config():
    """Конфигурация с наложенными настройками из приватного хранилища."""
    cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    override = storage.load_settings().get("config", {})
    def merge(a, b):
        if not isinstance(a, dict) or not isinstance(b, dict):
            return b
        out = dict(a)
        for k, v in b.items():
            out[k] = merge(out[k], v) if k in out else v
        return out
    return merge(cfg, override)


def tg(method, **params):
    params = {k: v for k, v in params.items() if v is not None}
    r = requests.post(f"{API}/{method}", json=params, timeout=70)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data)
    return data["result"]


def is_admin(user_id):
    return str(user_id) in ADMIN_IDS


def settings():
    data = storage.load_settings()
    data.setdefault("config", {})
    data.setdefault("pending", {})
    data.setdefault("active_regions", list(BASE_CFG.get("bot", {}).get("regions", {}).keys()))
    return data


def set_path(root, path, value):
    cur = root
    for key in path[:-1]:
        cur = cur.setdefault(key, {})
    cur[path[-1]] = value


def admin_kb():
    return {"inline_keyboard": [
        [{"text": "📰 Новости", "callback_data": "adm:news"},
         {"text": "🎭 Афиша", "callback_data": "adm:afisha"}],
        [{"text": "🌍 Регионы", "callback_data": "adm:regions"},
         {"text": "✍️ Оформление", "callback_data": "adm:style"}],
        [{"text": "🔢 Количество", "callback_data": "adm:count"},
         {"text": "🔄 Обновление", "callback_data": "adm:update"}],
    ]}


def admin_home(chat, edit=False, message_id=None):
    text = "⚙️ <b>Управление ботом</b>\n\nПривет! Что будем настраивать?"
    if edit and message_id:
        tg("editMessageText", chat_id=chat, message_id=message_id, text=text,
           parse_mode="HTML", reply_markup=admin_kb())
    else:
        tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML", reply_markup=admin_kb())


def send_admin_list(chat, title, rows, back="home"):
    keyboard = [[{"text": label, "callback_data": data}] for label, data in rows]
    keyboard.append([{"text": "⬅️ В меню", "callback_data": "adm:home"}])
    tg("sendMessage", chat_id=chat, text=title, reply_markup={"inline_keyboard": keyboard})


def prompt(chat, text, path, kind="text"):
    st = settings()
    st["pending"][str(chat)] = {"path": path, "kind": kind}
    storage.save_settings(st)
    tg("sendMessage", chat_id=chat, text=text + "\n\nОтправь новое значение сообщением. Для отмены напиши /cancel.")


def handle_admin_callback(cq):
    user_id = cq["from"]["id"]
    chat = cq["message"]["chat"]["id"]
    mid = cq["message"]["message_id"]
    data = cq.get("data", "")
    tg("answerCallbackQuery", callback_query_id=cq["id"])
    if not is_admin(user_id):
        return
    cfg = config()
    if data == "adm:home":
        admin_home(chat, True, mid)
    elif data == "adm:news":
        rows = [(f"📰 {name}", f"adm:topics:{name}") for name in cfg.get("digests", {})]
        send_admin_list(chat, "Выбери выпуск для редактирования источников и категорий:", rows)
    elif data.startswith("adm:topics:"):
        name = data.split(":", 2)[2]
        topics = cfg.get("digests", {}).get(name, {}).get("topics", [])
        rows = [(t.get("name", f"Раздел {i+1}"), f"adm:topic:{name}:{i}") for i, t in enumerate(topics)]
        send_admin_list(chat, f"Разделы выпуска «{name}»:\nВыбери раздел, чтобы изменить поисковые запросы.", rows)
    elif data.startswith("adm:topic:"):
        _, _, name, idx = data.split(":", 3)
        topic = cfg["digests"][name]["topics"][int(idx)]
        path = ["digests", name, "topics", int(idx), "queries"]
        prompt(chat, f"Текущие запросы раздела «{topic.get('name','')}»:\n" +
               "\n".join(topic.get("queries", [])) +
               "\n\nОтправь новые запросы, каждый с новой строки.", path, "lines")
    elif data == "adm:afisha":
        choices = [(f"🎭 {name}", f"adm:afishatopics:{name}") for name, d in cfg.get("digests", {}).items() if d.get("regional_topics")]
        send_admin_list(chat, "Выбери выпуск с региональной афишей:", choices)
    elif data.startswith("adm:afishatopics:"):
        name = data.split(":", 2)[2]
        topics = cfg["digests"][name].get("regional_topics", [])
        rows = [(t.get("name", f"Раздел {i+1}"), f"adm:afishatopic:{name}:{i}") for i, t in enumerate(topics)]
        send_admin_list(chat, "Выбери раздел афиши для редактирования запросов:", rows)
    elif data.startswith("adm:afishatopic:"):
        _, _, name, idx = data.split(":", 3)
        topic = cfg["digests"][name]["regional_topics"][int(idx)]
        prompt(chat, f"Текущие запросы «{topic.get('name','')}»:\n" +
               "\n".join(topic.get("queries", [])) +
               "\n\nОтправь новые запросы, каждый с новой строки.",
               ["digests", name, "regional_topics", int(idx), "queries"], "lines")
    elif data == "adm:regions":
        st = settings()
        active = set(st["active_regions"])
        rows = []
        for slug, region in cfg.get("bot", {}).get("regions", {}).items():
            mark = "✅" if slug in active else "⬜"
            rows.append((f"{mark} {region['name']}", f"adm:region:{slug}"))
        send_admin_list(chat, "Нажимай на регион, чтобы включать или отключать его в рассылке:", rows)
    elif data.startswith("adm:region:"):
        slug = data.split(":", 2)[2]
        st = settings()
        active = set(st["active_regions"])
        if slug in active:
            active.remove(slug)
        else:
            active.add(slug)
        st["active_regions"] = sorted(active)
        storage.save_settings(st)
        handle_admin_callback({"id": cq["id"], "from": cq["from"], "message": cq["message"], "data": "adm:regions"})
    elif data == "adm:style":
        rows = [("Изменить стиль текста", "adm:styletext"),
                ("Изменить язык", "adm:language"),
                ("Переключить эмодзи", "adm:emoji")]
        send_admin_list(chat, "Настройки оформления публикаций:", rows)
    elif data == "adm:styletext":
        prompt(chat, "Напиши новый стиль дайджеста (например: коротко, дружелюбно, без канцелярита).",
               ["defaults", "style"])
    elif data == "adm:language":
        prompt(chat, "Напиши язык публикаций:", ["defaults", "language"])
    elif data == "adm:emoji":
        st = settings()
        current = st["config"].get("defaults", {}).get("emoji_per_news", cfg["defaults"].get("emoji_per_news", True))
        set_path(st["config"], ["defaults", "emoji_per_news"], not current)
        storage.save_settings(st)
        tg("sendMessage", chat_id=chat, text=f"Эмодзи в новостях: {'включены' if not current else 'выключены'}.")
    elif data == "adm:count":
        prompt(chat, "Сколько новостей показывать в каждом разделе? Отправь целое число.",
               ["defaults", "news_per_topic"], "number")
    elif data == "adm:update":
        tg("sendMessage", chat_id=chat, text="🔄 Запускаю сбор новостей. Это может занять несколько минут…")
        result = subprocess.run([sys.executable, os.path.join(ROOT, "digest.py")],
                                cwd=ROOT, capture_output=True, text=True, timeout=1700)
        tail = (result.stdout + "\n" + result.stderr)[-3000:]
        tg("sendMessage", chat_id=chat,
           text=("✅ Сбор завершён." if result.returncode == 0 else "⚠️ Сбор завершился с ошибкой.") +
                ("\n\n" + tail if tail.strip() else ""))
    else:
        return


def handle(u, state):
    cq = u.get("callback_query")
    if cq:
        if cq.get("data", "").startswith("adm:"):
            handle_admin_callback(cq)
            return False
        tg("answerCallbackQuery", callback_query_id=cq["id"])
        data, chat = cq.get("data", ""), cq["message"]["chat"]["id"]
        cfg = config()
        regions = cfg.get("bot", {}).get("regions", {})
        active = set(settings()["active_regions"])
        if data.startswith("r:") and data[2:] in regions and data[2:] in active:
            old = state["users"].get(str(chat), {})
            state["users"][str(chat)] = {"region": data[2:], "since": old.get("since") or datetime.now().strftime("%Y-%m-%d")}
            tg("sendMessage", chat_id=chat, text=cfg["bot"]["welcome_text"].format(region=regions[data[2:]]["name"]))
            return True
        return False

    msg = u.get("message") or {}
    if (msg.get("chat") or {}).get("type") != "private":
        return False
    text, chat = msg.get("text", ""), msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id")
    if not text:
        return False

    st = settings()
    pending = st["pending"].get(str(chat))
    if pending and not text.startswith("/"):
        value = text.strip()
        if pending["kind"] == "lines":
            value = [line.strip() for line in value.splitlines() if line.strip()]
            if not value:
                tg("sendMessage", chat_id=chat, text="Список пустой — ничего не изменено.")
                return False
        elif pending["kind"] == "number":
            try:
                value = int(value)
                if not 1 <= value <= 30:
                    raise ValueError()
            except ValueError:
                tg("sendMessage", chat_id=chat, text="Нужно целое число от 1 до 30. Попробуй ещё раз или /cancel.")
                return False
        set_path(st["config"], pending["path"], value)
        st["pending"].pop(str(chat), None)
        storage.save_settings(st)
        tg("sendMessage", chat_id=chat, text="✅ Сохранено! Изменения применятся к следующим выпускам.")
        return False

    if not text.startswith("/"):
        return False
    parts = text.split(maxsplit=1)
    cmd = parts[0].split("@")[0]
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd == "/admin":
        if is_admin(user_id):
            admin_home(chat)
        else:
            print("Blocked non-admin /admin from", user_id)
        return False
    if cmd == "/cancel":
        if str(chat) in st["pending"]:
            st["pending"].pop(str(chat), None)
            storage.save_settings(st)
            tg("sendMessage", chat_id=chat, text="Редактирование отменено.")
        return False

    cfg = config()
    regions = cfg.get("bot", {}).get("regions", {})
    active = set(settings()["active_regions"])
    if cmd == "/start" and arg in regions and arg in active:
        old = state["users"].get(str(chat), {})
        state["users"][str(chat)] = {"region": arg, "since": old.get("since") or datetime.now().strftime("%Y-%m-%d")}
        tg("sendMessage", chat_id=chat, text=cfg["bot"]["welcome_text"].format(region=regions[arg]["name"]))
        return True
    if cmd in ("/start", "/regions"):
        cur = state["users"].get(str(chat))
        note = f"\nСейчас выбран: {regions[cur['region']]['name']}" if cur and cur.get("region") in regions else ""
        rows = [[{"text": f"📍 {r['name']}", "callback_data": f"r:{slug}"}]
                for slug, r in regions.items() if slug in active]
        tg("sendMessage", chat_id=chat, text=cfg["bot"]["choose_text"] + note,
           reply_markup={"inline_keyboard": rows})
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
            state = storage.load()
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
            storage.save(state)
        if once:
            if updates:
                tg("getUpdates", offset=offset, timeout=0)
            return


def post_menu():
    cfg = config()
    username = tg("getMe")["username"]
    chat = cfg["bot"].get("channel") or os.environ["TG_CHAT_ID"]
    rows = [[{"text": f"📍 {r['name']}", "url": f"https://t.me/{username}?start={slug}"}]
            for slug, r in cfg["bot"]["regions"].items() if slug in settings()["active_regions"]]
    tg("sendMessage", chat_id=chat, text=cfg["bot"]["menu_text"],
       reply_markup={"inline_keyboard": rows})
    print("Меню опубликовано")


if __name__ == "__main__":
    if "--post-menu" in sys.argv:
        post_menu()
    else:
        if os.getenv("GITHUB_ACTIONS") and not storage.configured():
            sys.exit("Не заданы секреты GIST_ID и GIST_TOKEN: без них настройки и подписки не сохранятся")
        poll(once="--once" in sys.argv)
