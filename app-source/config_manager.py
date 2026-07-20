"""Configuration Manager — Read/Write config.json with locking."""
import json, os, threading

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config.json")
_lock = threading.Lock()


def load_config():
    with _lock:
        with open(CONFIG_PATH) as f:
            return json.load(f)


def save_config(c):
    with _lock:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                old = f.read()
            with open(CONFIG_PATH + ".bak", "w") as f:
                f.write(old)
        with open(CONFIG_PATH, "w") as f:
            json.dump(c, f, indent=4)
    return True


def add_site(name, path, enabled=True):
    c = load_config()
    c["sites"].append({"name": name, "path": path, "enabled": enabled})
    return save_config(c)


def remove_site(i):
    c = load_config()
    if 0 <= i < len(c["sites"]):
        c["sites"].pop(i)
        return save_config(c)
    return False


def toggle_site(i):
    c = load_config()
    if 0 <= i < len(c["sites"]):
        c["sites"][i]["enabled"] = not c["sites"][i]["enabled"]
        return save_config(c)
    return False
