"""Новостной дайджест по расписанию: RSS -> LLM-сжатие -> Telegram + e-mail.
Все настройки в config.yaml. Запуск: `python digest.py` (все дайджесты на сегодня)
или `python digest.py weekly` (конкретный, независимо от дня).
`python digest.py --scheduled` запускают каждый час (GitHub Actions): сам проверяет, пора ли слать
(время send_time из меню бота или config.yaml, окно 3 часа, раз в день), и если не пора, тихо выходит."""
import os, re, sys, json, time, html, smtplib, calendar
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from urllib.parse import quote
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

import settings  # первым: подхватывает .env
import storage

LLM_BASE_URL = os.getenv("LLM_BASE_URL") or "https://api.groq.com/openai/v1"
LLM_API_KEY = os.environ["LLM_API_KEY"]
# llama-3.3-70b-versatile у Groq отключена, поэтому по умолчанию gpt-oss-120b
LLM_MODEL = os.getenv("LLM_MODEL") or "openai/gpt-oss-120b"

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]

PROMPT = """Ты редактор новостной подборки для Telegram-паблика. Раздел: «{topic}».
Ниже новости за период. Выбери {n} самых важных и интересных, дубликаты объедини.
Язык: {language}. Стиль: {style}.

Формат каждой новости СТРОГО такой, новости разделяй пустой строкой:
{emoji_part}<b>Яркий заголовок</b> — суть в 1-2 предложениях. 🔗 <a href="URL">источник</a>

Правила:
- разрешены только теги <b> и <a>, никакого markdown;
- в качестве текста ссылки используй название источника из списка;
- URL бери ровно из списка, ничего не выдумывай и не добавляй фактов, которых там нет.

Новости:
{items}"""

TAG_RE = re.compile(r'<(?:/?b|a href="[^"<>]+"|/a)>')


def clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def sanitize(text):
    """Оставляем только <b> и <a href>, остальное экранируем, чтобы Telegram не ругался."""
    out, pos = [], 0
    for m in TAG_RE.finditer(text):
        out.append(html.escape(html.unescape(text[pos:m.start()]), quote=False))
        out.append(m.group(0))
        pos = m.end()
    out.append(html.escape(html.unescape(text[pos:]), quote=False))
    res = "".join(out)
    if res.count("<b>") != res.count("</b>") or res.count("<a ") != res.count("</a>"):
        res = re.sub(r"<[^>]+>", "", res)  # теги не сошлись: убираем их все
    return res


def fetch_topic(topic, d):
    days = d["period_days"]
    urls = [
        f"https://news.google.com/rss/search?q={quote(q)}+when:{days}d&hl=ru&gl=RU&ceid=RU:ru"
        for q in topic.get("queries", [])
    ] + topic.get("feeds", [])
    cutoff = time.time() - days * 86400
    seen, items = set(), []
    for url in urls:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print("RSS error", url, e)
            continue
        for e in feed.entries:
            ts = calendar.timegm(e.published_parsed) if e.get("published_parsed") else time.time()
            title = clean(e.get("title"))
            key = title.lower()[:60]
            if ts < cutoff or key in seen or not title:
                continue
            seen.add(key)
            source = (e.get("source") or {}).get("title") or feed.feed.get("title", "источник")
            items.append((ts, title, source, e.get("link", ""), clean(e.get("summary"))[:200]))
    items.sort(reverse=True)
    return items[: d["max_items_per_topic"]]


def llm_call(prompt):
    """Запрос к модели; при лимите (429) ждём и повторяем."""
    for attempt in range(6):
        r = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={"model": LLM_MODEL, "temperature": 0.4,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=120,
        )
        if r.status_code == 429:
            try:
                wait = float(r.headers.get("retry-after", ""))
            except ValueError:
                wait = 20 * (attempt + 1)
            wait = min(max(wait, 5), 90)
            print(f"Лимит модели (429), жду {wait:.0f} с и повторяю")
            time.sleep(wait)
            continue
        if r.status_code >= 400:
            print("LLM ответ:", r.text[:300])
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    raise RuntimeError("модель не ответила из-за лимитов")


def summarize(topic, items, d):
    lines = []
    for _, title, source, link, desc in items:
        extra = f" | {desc}" if desc and desc.lower() != title.lower() else ""
        lines.append(f"- {title}{extra} | источник: {source} | {link}")
    prompt = PROMPT.format(
        topic=topic["name"], n=d["news_per_topic"], language=d["language"],
        style=d["style"] + (". " + topic["hint"] if topic.get("hint") else ""),
        emoji_part="<эмодзи по смыслу> " if d["emoji_per_news"] else "",
        items="\n".join(lines))
    return sanitize(llm_call(prompt))


def build_blocks(d, now):
    blocks = []
    for topic in d["topics"]:
        items = fetch_topic(topic, d)
        if not items:
            continue
        try:
            body = summarize(topic, items, d)
        except Exception as e:
            print("LLM error", topic["name"], e)
            continue
        blocks.append(f"{topic.get('emoji', '📌')} <b>{html.escape(topic['name'])}</b>\n\n{body}")
        time.sleep(d.get("pause_seconds", 8))  # пауза, чтобы не упереться в лимиты
    if blocks and d.get("footer"):
        blocks.append(html.escape(d["footer"]))
    if blocks:
        head = f"📰 <b>{html.escape(d['title'])}</b>"
        if d["show_date"]:
            head += f" — {WEEKDAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]} {now.year}"
        blocks[0] = head + "\n\n" + blocks[0]
    return blocks


def tg_send(token, chat, text):
    """Отправка в Telegram; если HTML не принят, шлём простым текстом."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                                 "disable_web_page_preview": True}, timeout=30)
    if r.status_code == 200:
        return
    print("Telegram ответил:", r.status_code, r.text[:300])
    plain = html.unescape(re.sub(r"<[^>]+>", "", text))
    r2 = requests.post(url, json={"chat_id": chat, "text": plain,
                                  "disable_web_page_preview": True}, timeout=30)
    if r2.status_code != 200:
        print("Telegram (простой текст) ответил:", r2.status_code, r2.text[:300])
        r2.raise_for_status()


def chunk(blocks):
    """Режем подборку на сообщения до ~4000 символов (лимит Telegram 4096), по разделам."""
    messages, cur = [], ""
    for b in blocks:
        if len(cur) + len(b) + 2 > 3900 and cur:
            messages.append(cur.strip())
            cur = ""
        cur += b + "\n\n"
    if cur.strip():
        messages.append(cur.strip())
    return messages


def send_telegram(blocks, d):
    token = os.getenv("TG_TOKEN")
    chat = d.get("telegram_chat") or os.getenv("TG_CHAT_ID")
    if not (token and chat):
        return print("Telegram не настроен, пропускаю")
    for m in chunk(blocks):
        tg_send(token, chat, m)
        time.sleep(1)


def regional_digest(d, reg, now):
    """Подборка для одного региона по шаблонам regional_topics ({region} и {city} подставляются)."""
    topics = []
    for t in d["regional_topics"]:
        topics.append({**t,
                       "queries": [q.format(region=reg["name"], city=reg["city"]) for q in t["queries"]],
                       "feeds": list(t.get("feeds", [])) + (list(reg.get("feeds", [])) if t.get("region_feeds") else [])})
    title = f"{d.get('regional_title', d['title'])} — {reg['name']}"
    return build_blocks({**d, "topics": topics, "title": title}, now)


def deliver_to_subscribers(d, channel_blocks, now, cfg):
    """Личная рассылка подписчикам бота: global = та же подборка, regional = своя для каждого региона."""
    mode = d.get("subscribers")
    if not mode:
        return True
    if os.getenv("GITHUB_ACTIONS") and not storage.configured():
        print("Подписчики: не заданы секреты GIST_ID и GIST_TOKEN, пропускаю")
        return False
    token = os.getenv("TG_TOKEN")
    users = storage.load()["users"]
    if not users:
        print("Подписчиков пока нет")
        return True
    regions = cfg.get("bot", {}).get("regions", {})
    groups = {}
    for uid, u in users.items():
        groups.setdefault(u.get("region") if mode == "regional" else "*", []).append(uid)
    blocked, ok = [], True
    for key, uids in groups.items():
        if mode == "regional":
            reg = regions.get(key)
            if not reg:
                continue
            try:
                blocks = regional_digest(d, reg, now)
            except Exception as e:
                print("Ошибка сборки для региона", key, e)
                ok = False
                continue
        else:
            blocks = channel_blocks
        if not blocks:
            print("Нет новостей для", key)
            continue
        messages = chunk(blocks)
        for uid in uids:
            try:
                for m in messages:
                    tg_send(token, uid, m)
                    time.sleep(0.5)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 403:
                    blocked.append(uid)  # человек заблокировал бота: убираем из подписчиков
                else:
                    print("Ошибка отправки подписчику", uid, e)
                    ok = False
            time.sleep(0.3)
    if blocked:
        state = storage.load()
        for uid in blocked:
            state["users"].pop(uid, None)
        storage.save(state)
        print("Удалены заблокировавшие бота:", len(blocked))
    return ok


def send_email(blocks, d, now):
    host, user, pwd = (os.getenv(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"))
    to = d.get("mail_to") or os.getenv("MAIL_TO")
    if not all((host, user, pwd, to)):
        return print("Почта не настроена, пропускаю")
    body = "<br><br>".join(b.replace("\n", "<br>") for b in blocks)
    msg = MIMEText(
        f"<html><body style='font-family:sans-serif;max-width:640px;line-height:1.5'>{body}</body></html>",
        "html", "utf-8")
    msg["Subject"] = f"{d['title']} — {now.strftime('%d.%m.%Y')}"
    msg["From"], msg["To"] = user, to
    with smtplib.SMTP_SSL(host, int(os.getenv("SMTP_PORT") or "465")) as s:
        s.login(user, pwd)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())


def tell_admin(text):
    """Короткое сообщение главному админу (первый ID из TG_ADMIN_ID). Без секрета молча пропускаем."""
    token = os.getenv("TG_TOKEN")
    admin = next((x for x in re.split(r"[,;\s]+", os.getenv("TG_ADMIN_ID", "")) if x), "")
    if not (token and admin):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": admin, "text": text, "disable_web_page_preview": True}, timeout=30)
    except Exception as e:
        print("Не смогла написать админу:", e)


DRAFTS = os.path.join(settings.BASE, "drafts.json")


def load_drafts():
    try:
        with open(DRAFTS, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_drafts(dr):
    with open(DRAFTS, "w", encoding="utf-8") as f:
        json.dump(dr, f, ensure_ascii=False, indent=1)


def deliver(name, d, blocks, now, cfg):
    """Реальная отправка: канал, почта, личная рассылка подписчикам."""
    ok = True
    if not blocks:
        print("Нет новостей за период для канала")
        ok = False
    else:
        for channel, fn in (("telegram", lambda: send_telegram(blocks, d)),
                            ("email", lambda: send_email(blocks, d, now))):
            if channel in d["channels"]:
                try:
                    fn()
                except Exception as e:
                    print(f"Ошибка отправки ({channel}):", e)
                    ok = False
    try:
        ok = deliver_to_subscribers(d, blocks, now, cfg) and ok
    except Exception as e:
        print("Ошибка личной рассылки:", e)
        ok = False
    return ok


def run_digest(name, d, now, cfg, mode="send"):
    """mode=send: собрать и сразу отправить. mode=draft: собрать и сохранить черновик в drafts.json."""
    print(f"== {name}")
    blocks = build_blocks(d, now)
    if mode == "draft":
        if not blocks:
            print("Нет новостей за период, черновик не создан")
            return False
        dr = load_drafts()
        dr[name] = {"blocks": blocks, "created": now.isoformat()}
        save_drafts(dr)
        print("Черновик готов:", name)
        return True
    return deliver(name, d, blocks, now, cfg)


def publish(name, d, now, cfg):
    """Отправить ранее собранный и одобренный черновик."""
    print(f"== публикация {name}")
    dr = load_drafts()
    item = dr.get(name)
    if not item:
        print("Черновика нет, соберите заново")
        return False
    ok = deliver(name, d, item["blocks"], now, cfg)
    if ok:
        dr.pop(name, None)
        save_drafts(dr)
    return ok


if __name__ == "__main__":
    cfg = settings.effective_cfg()  # config.yaml + то, что настроено кнопками в боте
    defaults = cfg.get("defaults", {})
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    pos = [a for a in sys.argv[1:] if not a.startswith("--")]
    only = pos[0] if pos else None
    mode = "publish" if "--publish" in flags else "draft" if "--draft" in flags else "send"
    now = datetime.now(ZoneInfo(defaults.get("timezone", "Asia/Yekaterinburg")))
    if "--scheduled" in flags:
        st = settings.load()
        hh, mm = map(int, str(defaults.get("send_time") or "09:00").split(":"))
        start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        today = now.strftime("%Y-%m-%d")
        if st.get("last_run") == today:
            sys.exit(print("Сегодня уже отправляла, выхожу"))
        if not (start <= now < start + timedelta(hours=3)):
            sys.exit(print(f"Не время: сейчас {now:%H:%M}, рассылка в {hh:02d}:{mm:02d} (окно 3 часа)"))
        # сначала отмечаем «сегодня запущено» (если отметить не вышло, ничего не шлём: следующий час попробует снова)
        settings.update(lambda x: x.__setitem__("last_run", today))
        print("Пора слать:", now.isoformat())
    failed, matched, ran = False, False, []
    for name, raw in cfg["digests"].items():
        d = {**defaults, **raw}
        if only:
            if name != only:
                continue
        elif d.get("days") and DAY_KEYS[now.weekday()] not in d["days"]:
            continue  # сегодня не его день
        matched = True
        ok = publish(name, d, now, cfg) if mode == "publish" else run_digest(name, d, now, cfg, mode)
        failed = failed or not ok
        if ok:
            ran.append(d["title"])
    if only and not matched:
        print("Дайджест не найден или выключен:", only)
        failed = True
    if ran and mode == "send" and os.getenv("GITHUB_ACTIONS"):
        # чтобы не заглядывать в Actions: видно, что автоматическая (или ручная) рассылка ушла
        tell_admin("✅ Рассылка отправлена: " + "; ".join(ran) + (" (часть выпусков не ушла, смотри Actions)" if failed else ""))
    if failed:
        sys.exit(1)
