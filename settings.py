"""Настройки, которые меняются кнопками в Telegram.
Лежат в settings.json рядом со скриптами. config.yaml остаётся «заводскими» настройками,
а settings.json поверх него включает/выключает разделы, регионы, рассылки и меняет параметры."""
import os, json, copy, threading, yaml

BASE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(BASE, "settings.json")
_lock = threading.RLock()


def load_env():
    """Читает файл .env рядом со скриптами (строки KEY=VALUE) в переменные окружения.
    Нужно, если панель сервера не передаёт свои переменные в Python."""
    try:
        with open(os.path.join(BASE, ".env"), encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.replace("export ", "").strip()
                v = v.strip().strip('"').strip("'")
                if k and v and not os.environ.get(k):
                    os.environ[k] = v
    except FileNotFoundError:
        pass


load_env()
EMPTY = {"overrides": {}, "off_topics": [], "off_digests": [], "off_regions": [], "last_run": "", "approval": True, "admins": []}


def raw_cfg():
    with open(os.path.join(BASE, "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def load():
    s = copy.deepcopy(EMPTY)
    try:
        with open(PATH, encoding="utf-8") as f:
            s.update(json.load(f))
    except (FileNotFoundError, ValueError):
        pass
    return s


def save(s):
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)


def update(fn):
    """Прочитать, изменить функцией fn(s), сохранить."""
    with _lock:
        s = load()
        fn(s)
        save(s)
        return s


def topic_key(digest_name, topic_name):
    return f"{digest_name}|{topic_name}"


def effective_cfg():
    """config.yaml + то, что настроено кнопками. Его читают и digest.py, и бот."""
    cfg = raw_cfg()
    s = load()
    ov = {k: v for k, v in s["overrides"].items() if v is not None}
    d = cfg.setdefault("defaults", {})
    d.update(ov)
    if "news_per_topic" in ov:  # чтобы было из чего выбирать
        d["max_items_per_topic"] = max(d.get("max_items_per_topic", 0), ov["news_per_topic"] * 4)
    digests = {}
    for name, raw in cfg["digests"].items():
        if name in s["off_digests"]:
            continue
        raw = dict(raw)
        for k in ov:  # настройка кнопками важнее, чем значение внутри отдельного дайджеста
            raw.pop(k, None)
        if "topics" in raw:
            raw["topics"] = [t for t in raw["topics"] if topic_key(name, t["name"]) not in s["off_topics"]]
        digests[name] = raw
    cfg["digests"] = digests
    regs = cfg.get("bot", {}).get("regions", {})
    if regs:
        cfg["bot"]["regions"] = {k: v for k, v in regs.items() if k not in s["off_regions"]}
    return cfg
