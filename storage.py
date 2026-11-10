"""Хранилище подписчиков и настроек: секретный Gist или локальные файлы."""
import json, os, requests

FILE = "subscribers.json"
SETTINGS_FILE = "admin_settings.json"
LOCAL = os.getenv("SUBSCRIBERS_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), FILE)
LOCAL_SETTINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), SETTINGS_FILE)


def _gist():
    gid, token = os.getenv("GIST_ID"), os.getenv("GIST_TOKEN")
    if gid and token:
        return (f"https://api.github.com/gists/{gid}",
                {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    return None


def configured():
    return _gist() is not None


def _gist_file(name):
    g = _gist()
    if not g:
        return None
    url, headers = g
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    f = r.json().get("files", {}).get(name)
    if not f:
        return {}
    content = f.get("content")
    if f.get("truncated"):
        content = requests.get(f["raw_url"], headers=headers, timeout=30).text
    try:
        return json.loads(content or "{}")
    except ValueError:
        return {}


def _gist_save(name, data):
    g = _gist()
    if not g:
        return False
    url, headers = g
    r = requests.patch(url, headers=headers,
                       json={"files": {name: {"content": json.dumps(data, ensure_ascii=False, indent=1)}}},
                       timeout=30)
    r.raise_for_status()
    return True


def _normalize(data):
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("users"), dict):
        data["users"] = {}
    return data


def load():
    if _gist():
        return _normalize(_gist_file(FILE))
    if os.path.exists(LOCAL):
        try:
            return _normalize(json.load(open(LOCAL, encoding="utf-8")))
        except ValueError:
            pass
    return _normalize({})


def save(data):
    if _gist_save(FILE, data):
        return
    with open(LOCAL, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def load_settings():
    if _gist():
        data = _gist_file(SETTINGS_FILE)
        return data if isinstance(data, dict) else {}
    if os.path.exists(LOCAL_SETTINGS):
        try:
            return json.load(open(LOCAL_SETTINGS, encoding="utf-8"))
        except ValueError:
            pass
    return {}


def save_settings(data):
    if _gist_save(SETTINGS_FILE, data):
        return
    with open(LOCAL_SETTINGS, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
