"""Configuration Manager — Read/Write config.json with locking."""
import fcntl
import json, os, threading

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config.json")
_lock = threading.Lock()


def load_config():
    with _lock:
        with open(CONFIG_PATH) as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def _write_locked(handle, config, old):
    with open(CONFIG_PATH + ".bak", "w") as backup:
        backup.write(old)
    handle.seek(0)
    json.dump(config, handle, indent=4)
    handle.truncate()
    handle.flush()
    os.fsync(handle.fileno())


def save_config(c):
    with _lock:
        with open(CONFIG_PATH, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                _write_locked(f, c, f.read())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    return True


def update_config(mutator):
    with _lock:
        with open(CONFIG_PATH, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                old = f.read()
                config = json.loads(old)
                result = mutator(config)
                _write_locked(f, config, old)
                return result
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def add_site(name, path, enabled=True):
    def mutate(config):
        normalized = str(path or "").strip().strip("/").lower()
        if any(str(site.get("path") or "").strip().strip("/").lower() == normalized for site in config.get("sites", [])):
            raise ValueError("Site path is already configured")
        config.setdefault("sites", []).append({"name": name, "path": path, "enabled": enabled})
        return True
    return update_config(mutate)


def remove_site(i):
    def mutate(config):
        if 0 <= i < len(config["sites"]):
            config["sites"].pop(i)
            return True
        return False
    return update_config(mutate)


def toggle_site(i):
    def mutate(config):
        if 0 <= i < len(config["sites"]):
            config["sites"][i]["enabled"] = not config["sites"][i]["enabled"]
            return True
        return False
    return update_config(mutate)
