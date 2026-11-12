"""Бот: подписка по регионам + админ-меню настроек в Telegram + расписание рассылки.
  python bot.py --post-menu   # запостить в паблик сообщение с кнопками регионов
  python bot.py               # слушать бота постоянно (на сервере) + сам запускать дайджест по расписанию
  python bot.py --once        # обработать накопившиеся нажатия и выйти (для GitHub Actions)
  python bot.py --listen 15   # отвечать мгновенно 15 минут и выйти (GitHub Actions, workflow «Bot live»)
Переменные: TG_TOKEN, TG_ADMIN_ID (твой Telegram ID; узнать: написать боту /myid)."""
import os, re, sys, json, time, html, threading, subprocess, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import settings
import storage

TOKEN = os.environ["TG_TOKEN"]
ADMIN_ID = os.getenv("TG_ADMIN_ID", "").strip()
ADMINS = [x for x in re.split(r"[,;\s]+", ADMIN_ID) if x]  # можно несколько ID через запятую
API = f"https://api.telegram.org/bot{TOKEN}"
BASE = settings.BASE
DRAFTS = os.path.join(BASE, "drafts.json")
ON_GITHUB = bool(os.getenv("GITHUB_ACTIONS"))


def dispatch_digest(name):
    """На GitHub: запустить workflow «News digest» вручную (name пустое = всё, что положено на сегодня)."""
    repo, token = os.getenv("GITHUB_REPOSITORY"), os.getenv("GH_TOKEN")
    if not (repo and token):
        raise RuntimeError("нет GITHUB_REPOSITORY или GH_TOKEN")
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    base = f"https://api.github.com/repos/{repo}/actions/workflows"
    r = requests.get(base, headers=h, timeout=30)
    r.raise_for_status()
    wf = next((w for w in r.json().get("workflows", []) if w.get("name") == "News digest"), None)
    if not wf:
        raise RuntimeError("не нашла workflow «News digest»")
    r = requests.post(f"{base}/{wf['id']}/dispatches", headers=h, timeout=30,
                      json={"ref": os.getenv("GITHUB_REF_NAME") or "main", "inputs": {"digest": name}})
    r.raise_for_status()


def admins():
    """Главные админы (из TG_ADMIN_ID) + добавленные через /addadmin."""
    return ADMINS + [x for x in settings.load().get("admins", []) if x not in ADMINS]


def is_admin(uid):
    return str(uid) in admins()


def who(uid):
    """Имя человека, если он уже писал боту."""
    try:
        c = tg("getChat", chat_id=uid)
        name = c.get("first_name") or ""
        return (name + (f" @{c['username']}" if c.get("username") else "")).strip()
    except Exception:
        return ""


def add_admin(target):
    settings.update(lambda s: s.__setitem__("admins", s["admins"] + [target]) if target not in s["admins"] else None)


def remove_admin(target):
    settings.update(lambda s: s.__setitem__("admins", [x for x in s["admins"] if x != target]))
KEYS = ("news_per_topic", "emoji_per_news", "show_date", "send_time")


def tg(method, **params):
    params = {k: v for k, v in params.items() if v is not None}
    r = requests.post(f"{API}/{method}", json=params, timeout=70)
    r.raise_for_status()
    return r.json()["result"]


# ---------- регионы и подписки ----------

def bot_cfg():
    return settings.raw_cfg()["bot"]


def regions():
    return settings.effective_cfg().get("bot", {}).get("regions", {})


def menu_keyboard(url_buttons=False, username=None):
    rows = []
    for slug, r in regions().items():
        text = f"📍 {r['name']}"
        if url_buttons:
            rows.append([{"text": text, "url": f"https://t.me/{username}?start={slug}"}])
        else:
            rows.append([{"text": text, "callback_data": f"r:{slug}"}])
    return {"inline_keyboard": rows}


def subscribe(state, chat, slug):
    regs = regions()
    if slug not in regs:
        tg("sendMessage", chat_id=chat, text="Этот регион сейчас недоступен. Выбрать другой: /start")
        return False
    old = state["users"].get(str(chat), {})
    state["users"][str(chat)] = {"region": slug, "since": old.get("since") or datetime.now().strftime("%Y-%m-%d")}
    tg("sendMessage", chat_id=chat, text=bot_cfg()["welcome_text"].format(region=regs[slug]["name"]))
    return True


# ---------- админ-меню ----------

def btn(text, data):
    return {"text": text, "callback_data": data}


def onoff(flag):
    return "✅" if flag else "⬜"


BACK = [[btn("⬅️ Назад", "a:main")]]


def flat_topics(raw):
    return [(dn, t) for dn, dd in raw["digests"].items() for t in dd.get("topics", [])]


def current(raw, s):
    d = raw.get("defaults", {})
    ov = {k: d.get(k) for k in KEYS}
    ov.update(s["overrides"])
    ov["send_time"] = ov.get("send_time") or "09:00"
    return ov


def screen(name, uid=""):
    raw, s = settings.raw_cfg(), settings.load()
    ov = current(raw, s)
    digests = list(raw["digests"].items())

    if name == "news":
        rows = []
        for i, (dn, t) in enumerate(flat_topics(raw)):
            on = settings.topic_key(dn, t["name"]) not in s["off_topics"]
            rows.append([btn(f"{onoff(on)} {t.get('emoji', '📌')} {t['name']} · {dn}", f"a:t:{i}")])
        return "📰 Новости\nКакие разделы включать в рассылку:", rows + BACK

    if name == "dig":
        rows = []
        for i, (dn, dd) in enumerate(digests):
            days = ", ".join(dd.get("days") or raw.get("defaults", {}).get("days") or []) or "каждый день"
            rows.append([btn(f"{onoff(dn not in s['off_digests'])} {dn} ({days})", f"a:d:{i}")])
        rows.append([btn(f"{onoff(s.get('approval', True))} Сначала присылать мне на проверку", "a:ap")])
        rows.append([btn(f"🕘 Время отправки: {ov['send_time']}", "a:time")])
        return "🎭 Рассылки\nКакие дайджесты отправлять и во сколько:", rows + BACK

    if name == "time":
        opts = ["07:00", "08:00", "09:00", "10:00", "12:00", "18:00"]
        rows = [[btn(("✅ " if o == ov["send_time"] else "") + o, f"a:tm:{o}") for o in opts[i:i + 3]]
                for i in range(0, len(opts), 3)]
        return "🕘 Во сколько отправлять (по времени из config.yaml)", rows + [[btn("⬅️ Назад", "a:dig")]]

    if name == "reg":
        rows = [[btn(f"{onoff(slug not in s['off_regions'])} {r['name']}", f"a:g:{slug}")]
                for slug, r in raw.get("bot", {}).get("regions", {}).items()]
        return "🌍 Регионы\nЧто показывать в меню выбора региона:", rows + BACK

    if name == "look":
        rows = [[btn(f"{onoff(ov.get('emoji_per_news'))} Эмодзи у каждой новости", "a:o:emoji_per_news")],
                [btn(f"{onoff(ov.get('show_date'))} Дата в заголовке", "a:o:show_date")]]
        return "✍️ Оформление публикаций", rows + BACK

    if name == "num":
        cur = ov.get("news_per_topic")
        rows = [[btn(("✅ " if cur == n else "") + str(n), f"a:n:{n}") for n in (3, 5, 7, 10)]]
        return f"🔢 Сколько новостей в каждом разделе\nСейчас: {cur}", rows + BACK

    if name == "adm":
        owner = str(uid) in ADMINS
        lines = [f"👑 {a} {who(a)}".strip() for a in ADMINS]
        extra = [x for x in s.get("admins", []) if x not in ADMINS]
        lines += [f"👤 {a} {who(a)}".strip() for a in extra]
        text = "👥 Админы\n\n" + "\n".join(lines) + "\n\n👑 главный, 👤 добавленный."
        if owner:
            text += "\nДобавить: /addadmin ID\nУбрать: кнопкой ниже или /removeadmin ID"
        rows = [[btn(f"❌ Убрать {a}", f"a:rm:{a}")] for a in extra] if owner else []
        return text, rows + BACK

    if name == "run":
        rows = [[btn(f"▶️ {dn}", f"a:go:{i}")] for i, (dn, _) in enumerate(digests)]
        rows.append([btn("▶️ Всё, что положено на сегодня", "a:go:today")])
        return "🔄 Обновление\nЗапустить сбор вручную (займёт несколько минут):", rows + BACK

    rows = [[btn("📰 Новости", "a:news"), btn("🎭 Рассылки", "a:dig")],
            [btn("🌍 Регионы", "a:reg"), btn("✍️ Оформление", "a:look")],
            [btn("🔢 Количество", "a:num"), btn("🔄 Обновление", "a:run")],
            [btn("👥 Админы", "a:adm")]]
    return "⚙️ Управление ботом\nПривет! Что будем настраивать?", rows


def show(chat, mid, name, uid=""):
    text, rows = screen(name, uid)
    try:
        tg("editMessageText", chat_id=chat, message_id=mid, text=text, reply_markup={"inline_keyboard": rows})
    except Exception as e:  # например «message is not modified»
        print("edit error", e)


def flip(lst, x):
    if x in lst:
        lst.remove(x)
    else:
        lst.append(x)


def admin_action(data, chat, mid, uid=""):
    act, _, arg = data.partition(":")
    raw, s = settings.raw_cfg(), settings.load()
    digests = list(raw["digests"].items())
    if act in ("main", "news", "dig", "time", "reg", "look", "num", "run", "adm"):
        return show(chat, mid, act, uid)
    if ON_GITHUB and act in ("go", "pub", "no"):
        # на GitHub бот живёт пару секунд, сам собирать новости не может: запускаем workflow
        if act == "go":
            try:
                dispatch_digest("" if arg == "today" else digests[int(arg)][0])
                text = ("⏳ Запустила сбор на GitHub. Рассылка уйдёт сразу в паблик и подписчикам, "
                        "обычно через несколько минут. Если не придёт: GitHub → Actions → News digest.")
            except Exception as e:
                text = f"⚠️ Не получилось запустить: {e}"
        else:
            text = "Черновики больше не используются: рассылка уходит сама."
        tg("sendMessage", chat_id=chat, text=text)
        return
    if act == "rm":
        if str(uid) in ADMINS:
            remove_admin(arg)
        return show(chat, mid, "adm", uid)
    if act == "t":
        dn, t = flat_topics(raw)[int(arg)]
        settings.update(lambda s: flip(s["off_topics"], settings.topic_key(dn, t["name"])))
        return show(chat, mid, "news")
    if act == "d":
        settings.update(lambda s: flip(s["off_digests"], digests[int(arg)][0]))
        return show(chat, mid, "dig")
    if act == "tm":
        settings.update(lambda s: s["overrides"].__setitem__("send_time", arg))
        return show(chat, mid, "dig")
    if act == "g":
        settings.update(lambda s: flip(s["off_regions"], arg))
        return show(chat, mid, "reg")
    if act == "o" and arg in ("emoji_per_news", "show_date"):
        new = not current(raw, s).get(arg)
        settings.update(lambda s: s["overrides"].__setitem__(arg, new))
        return show(chat, mid, "look")
    if act == "n":
        settings.update(lambda s: s["overrides"].__setitem__("news_per_topic", int(arg)))
        return show(chat, mid, "num")
    if act == "ap":
        settings.update(lambda s: s.__setitem__("approval", not s.get("approval", True)))
        return show(chat, mid, "dig")
    if act == "go":
        approval = s.get("approval", True)
        args = [] if arg == "today" else [digests[int(arg)][0]]
        if approval:
            args = ["--draft"] + args
        if run_digest_proc(args, chat, "✅ Отправлено"):
            tg("sendMessage", chat_id=chat, text=("⏳ Собираю черновик, это займёт несколько минут. Пришлю его на проверку."
                                                  if approval else "⏳ Запустила. Пришлю отчёт, когда закончу."))
        else:
            tg("sendMessage", chat_id=chat, text="Сбор уже идёт, подожди пару минут.")
        return
    if act in ("pub", "no"):
        name = digests[int(arg)][0]
        if act == "no":
            dr = load_drafts()
            dr.pop(name, None)
            save_drafts(dr)
            return edit_text(chat, mid, f"❌ «{name}»: отменено, в паблик ничего не ушло.")
        edit_text(chat, mid, f"⏳ Публикую «{name}»…")
        if not run_digest_proc(["--publish", name], chat, f"✅ «{name}» опубликован"):
            tg("sendMessage", chat_id=chat, text="Сейчас идёт другой сбор, нажми «Опубликовать» через пару минут.")
        return


# ---------- запуск дайджеста и расписание ----------

_run_lock = threading.Lock()


def load_drafts():
    try:
        with open(DRAFTS, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_drafts(dr):
    with open(DRAFTS, "w", encoding="utf-8") as f:
        json.dump(dr, f, ensure_ascii=False, indent=1)


def edit_text(chat, mid, text):
    try:
        tg("editMessageText", chat_id=chat, message_id=mid, text=text)
    except Exception as e:
        print("edit error", e)


def chunk(blocks):
    messages, cur = [], ""
    for b_ in blocks:
        if len(cur) + len(b_) + 2 > 3900 and cur:
            messages.append(cur.strip())
            cur = ""
        cur += b_ + "\n\n"
    if cur.strip():
        messages.append(cur.strip())
    return messages


def send_html(chat, text):
    try:
        tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        tg("sendMessage", chat_id=chat, text=html.unescape(re.sub(r"<[^>]+>", "", text)), disable_web_page_preview=True)


def send_drafts(chat):
    """Присылает админу черновики и кнопки «Опубликовать / Заново / Отмена»."""
    drafts = load_drafts()
    names = list(settings.raw_cfg()["digests"])
    shown = 0
    for name, dr in drafts.items():
        if name not in names:
            continue
        i = names.index(name)
        for m in chunk(dr["blocks"]):
            send_html(chat, m)
        kb = {"inline_keyboard": [[btn("✅ Опубликовать", f"a:pub:{i}")],
                                  [btn("🔄 Собрать заново", f"a:go:{i}"), btn("❌ Отмена", f"a:no:{i}")]]}
        tg("sendMessage", chat_id=chat, text=f"👆 Это черновик «{name}». Публикуем в паблик?", reply_markup=kb)
        shown += 1
    if not shown:
        tg("sendMessage", chat_id=chat, text="Черновиков нет: либо сегодня нет подходящего дайджеста, либо не нашлось новостей.")


def run_digest_proc(args, chat, ok_text="✅ Готово"):
    """digest.py в отдельном процессе, чтобы бот продолжал отвечать на кнопки."""
    if not _run_lock.acquire(blocking=False):
        return False

    def job():
        ok = False
        try:
            p = subprocess.run([sys.executable, os.path.join(BASE, "digest.py"), *args],
                               capture_output=True, text=True, cwd=BASE, timeout=3600)
            ok = p.returncode == 0
            out = (p.stdout + p.stderr).strip()[-700:]
            msg = ok_text if ok else "⚠️ Не получилось. Вот что пишет лог:\n\n" + out
        except Exception as e:
            msg = f"⚠️ Не удалось запустить сбор: {e}"
        finally:
            _run_lock.release()
        if not chat:
            return
        try:
            if ok and "--draft" in args:
                send_drafts(chat)
            else:
                tg("sendMessage", chat_id=chat, text=msg)
        except Exception as e:
            print("report error", e)

    threading.Thread(target=job, daemon=True).start()
    return True


def scheduler():
    """Раз в день в заданное время собирает дайджест (черновик на проверку или сразу отправка)."""
    while True:
        try:
            cfg, s = settings.effective_cfg(), settings.load()
            tz = ZoneInfo(cfg.get("defaults", {}).get("timezone", "Asia/Yekaterinburg"))
            now = datetime.now(tz)
            h, m = map(int, (s["overrides"].get("send_time") or cfg["defaults"].get("send_time") or "09:00").split(":"))
            start = now.replace(hour=h, minute=m, second=0, microsecond=0)
            today = now.strftime("%Y-%m-%d")
            if s.get("last_run") != today and start <= now < start + timedelta(hours=3):
                settings.update(lambda st: st.__setitem__("last_run", today))
                draft = s.get("approval", True) and bool(ADMINS)
                print("Расписание: запускаю", "черновик" if draft else "дайджест", now)
                run_digest_proc(["--draft"] if draft else [], ADMINS[0] if ADMINS else None, "✅ Дайджест отправлен")
        except Exception as e:
            print("scheduler error", e)
        time.sleep(30)


# ---------- обработка сообщений ----------

def handle(u, state):
    """Обрабатывает одно обновление. True, если список подписчиков изменился."""
    cq = u.get("callback_query")
    if cq:
        tg("answerCallbackQuery", callback_query_id=cq["id"])
        data, chat = cq.get("data", ""), cq["message"]["chat"]["id"]
        if data.startswith("a:"):
            if is_admin(cq["from"]["id"]):
                admin_action(data[2:], chat, cq["message"]["message_id"], cq["from"]["id"])
            return False
        if data.startswith("r:"):
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
    uid = str((msg.get("from") or {}).get("id", ""))
    if cmd == "/myid":
        tg("sendMessage", chat_id=chat, text=f"Твой Telegram ID: {uid}\nПропиши его в переменной TG_ADMIN_ID на сервере.")
        return False
    if cmd == "/admin":
        if not ADMINS:
            tg("sendMessage", chat_id=chat, text=f"Не задана переменная TG_ADMIN_ID. Твой ID: {uid}")
        elif is_admin(uid):
            text_, rows = screen("main")
            tg("sendMessage", chat_id=chat, text=text_, reply_markup={"inline_keyboard": rows})
        return False
    if cmd in ("/addadmin", "/removeadmin"):
        if uid not in ADMINS:  # управлять админами могут только главные
            return False
        target = re.sub(r"\D", "", arg)
        if not target:
            tg("sendMessage", chat_id=chat, text=f"Напиши так: {cmd} 123456789\nID человека он узнаёт командой /myid у бота.")
        elif cmd == "/addadmin":
            if target in admins():
                tg("sendMessage", chat_id=chat, text="Этот человек уже админ.")
            else:
                add_admin(target)
                note = ""
                try:
                    tg("sendMessage", chat_id=target, text="Тебя добавили админом бота. Напиши /admin, чтобы открыть меню.")
                except Exception:
                    note = "\nЕму пока не удалось написать: пусть нажмёт у бота «Старт», потом /admin."
                tg("sendMessage", chat_id=chat, text=f"✅ Админ {target} добавлен.{note}")
        elif target in ADMINS:
            tg("sendMessage", chat_id=chat, text="Главного админа убрать нельзя, он задан на сервере (TG_ADMIN_ID).")
        elif target in admins():
            remove_admin(target)
            tg("sendMessage", chat_id=chat, text=f"✅ Админ {target} убран.")
        else:
            tg("sendMessage", chat_id=chat, text="Такого админа нет в списке.")
        return False
    regs = regions()
    if cmd == "/start" and arg in regs:
        return subscribe(state, chat, arg)
    if cmd in ("/start", "/regions"):
        cur = state["users"].get(str(chat))
        note = f"\nСейчас выбран: {regs[cur['region']]['name']}" if cur and cur.get("region") in regs else ""
        tg("sendMessage", chat_id=chat, text=bot_cfg()["choose_text"] + note, reply_markup=menu_keyboard())
        return False
    if cmd == "/stop":
        if state["users"].pop(str(chat), None) is not None:
            tg("sendMessage", chat_id=chat, text="Вы отписались от рассылки. Чтобы подписаться снова, нажмите /start")
            return True
        tg("sendMessage", chat_id=chat, text="Вы и не были подписаны. Выбрать регион: /start")
    return False


def poll(once=False, until=None):
    """once: забрать накопившееся и выйти. until (time.time()): слушать до этого момента и выйти.
    Без обоих: слушать бесконечно (на своём сервере)."""
    offset = None
    while True:
        wait = 0 if once else 50
        if until:
            left = until - time.time()
            if left <= 0:
                if offset is not None:
                    tg("getUpdates", offset=offset, timeout=0)  # подтвердить уже обработанное
                return
            wait = max(1, int(min(25, left)))  # не меньше секунды, чтобы не долбить Telegram пустыми запросами
        try:
            updates = tg("getUpdates", offset=offset, timeout=wait,
                         allowed_updates=["message", "callback_query"])
            # подписчиков мог изменить digest.py, поэтому читаем заново, но только когда есть что обрабатывать
            state = storage.load() if updates else None
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
    chat = bot_cfg().get("channel") or os.environ["TG_CHAT_ID"]
    tg("sendMessage", chat_id=chat, text=bot_cfg()["menu_text"],
       reply_markup=menu_keyboard(url_buttons=True, username=username))
    print("Меню опубликовано")


if __name__ == "__main__":
    if "--post-menu" in sys.argv:
        post_menu()
    else:
        if os.getenv("GITHUB_ACTIONS") and not storage.configured():
            sys.exit("Не заданы секреты GIST_ID и GIST_TOKEN: без них подписки не сохранятся")
        once = "--once" in sys.argv
        minutes = None
        if "--listen" in sys.argv:  # режим «на связи»: отвечает мгновенно N минут и выходит (запускается вручную)
            i = sys.argv.index("--listen")
            try:
                minutes = min(float(sys.argv[i + 1]), 25)
            except (IndexError, ValueError):
                minutes = 15
        if not once and minutes is None and not os.getenv("GITHUB_ACTIONS"):
            threading.Thread(target=scheduler, daemon=True).start()
            print("Бот запущен, расписание включено")
        if minutes is not None:
            for a in ADMINS[:1]:
                try:
                    tg("sendMessage", chat_id=a, text=f"🟢 Бот на связи ещё {minutes:g} мин, отвечаю сразу. Открой /admin")
                except Exception as e:
                    print("hello error", e)
            poll(until=time.time() + minutes * 60)
            for a in ADMINS[:1]:
                try:
                    tg("sendMessage", chat_id=a, text="⚪ Бот ушёл в обычный режим: теперь отвечает с задержкой в несколько минут.")
                except Exception as e:
                    print("bye error", e)
        else:
            poll(once=once)
