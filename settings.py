"""Настройки, которые меняются кнопками в Telegram.
Если заданы секреты GIST_ID и GIST_TOKEN (так на GitHub), лежат в файле settings.json секретного Gist,
рядом с подписчиками, и переживают перезапуски. Иначе (свой компьютер, сервер) лежат в settings.json
рядом со скриптами. config.yaml остаётся «заводскими» настройками,
а settings.json поверх него включает/выключает разделы, регионы, рассылки и меняет параметры."""
import os, json, copy, threading, requests, yaml

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


def _gist():
    gid, token = os.getenv("GIST_ID"), os.getenv("GIST_TOKEN")
    if gid and token:
        return (f"https://api.github.com/gists/{gid}",
                {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    return None


def _read_local():
    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _read_gist(g):
    """Ошибки сети НЕ глушим: иначе при сбое настройки сбросились бы на заводские и перезаписались."""
    url, headers = g
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    f = r.json().get("files", {}).get("settings.json")
    if not f:
        return _read_local()  # в Gist настроек ещё нет: берём локальные, если они есть
    text = f.get("content")
    if f.get("truncated"):
        text = requests.get(f["raw_url"], headers=headers, timeout=30).text
    try:
        return json.loads(text or "{}")
    except ValueError:
        return {}


def load():
    s = copy.deepcopy(EMPTY)
    g = _gist()
    data = _read_gist(g) if g else _read_local()
    if isinstance(data, dict):
        s.update(data)
    return s


def save(s):
    g = _gist()
    if g:
        url, headers = g
        text = json.dumps(s, ensure_ascii=False, indent=1)
        r = requests.patch(url, headers=headers, json={"files": {"settings.json": {"content": text}}}, timeout=30)
        r.raise_for_status()
        return
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
