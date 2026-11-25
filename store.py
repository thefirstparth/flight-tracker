"""Where the tracker keeps its state, price history, alert log and latest
flight lists.

Two interchangeable backends:

- FileStore:  files in a folder (data/). Used on your own computer, in tests
              and by the GitHub Actions workflow.
- RedisStore: Upstash Redis over its REST API (no extra packages). Used on
              Vercel, where the disk is not kept between runs.

`open_store()` picks Redis when its environment variables are set
(added automatically when Upstash Redis is connected to a Vercel project),
otherwise files.
"""
from __future__ import annotations

import csv
import json
import os
import urllib.request
import uuid
from pathlib import Path

HISTORY_FIELDS = ["checked_at", "watch_id", "price", "flights", "status"]
EVENT_FIELDS = ["checked_at", "watch_id", "kind", "price", "reference"]
MAX_EVENTS = 500


def _write_json_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _append_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        if new:
            writer.writeheader()
        writer.writerows(rows)


class FileStore:
    """state.json, latest.json, history.csv and events.csv in one folder."""

    kind = "files"

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def load_state(self) -> dict:
        p = self.folder / "state.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def save_state(self, state: dict) -> None:
        _write_json_atomic(self.folder / "state.json", state)

    def load_latest(self) -> dict:
        p = self.folder / "latest.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def save_latest(self, latest: dict) -> None:
        _write_json_atomic(self.folder / "latest.json", latest)

    def add_history(self, rows: list[dict]) -> None:
        _append_csv(self.folder / "history.csv", rows, HISTORY_FIELDS)

    def load_history(self) -> list[dict]:
        return _read_csv(self.folder / "history.csv")

    def add_events(self, rows: list[dict]) -> None:
        _append_csv(self.folder / "events.csv", rows, EVENT_FIELDS)

    def load_events(self) -> list[dict]:
        return _read_csv(self.folder / "events.csv")[-MAX_EVENTS:]

    def acquire_lock(self, seconds: int) -> bool:
        return True

    def release_lock(self) -> None:
        pass


class RedisError(RuntimeError):
    pass


class RedisStore:
    """Upstash Redis via REST.

    History keeps only the checks where a watch's price (or found/not found)
    changed, which is all the dashboard's step charts need, so storage stays
    small no matter how often prices are checked.
    """

    kind = "redis"

    def __init__(self, url: str, token: str, prefix: str = "ft:", opener=None):
        self.url = url.rstrip("/")
        self.token = token
        self.p = prefix
        self._lock_id = None
        self._open = opener or urllib.request.urlopen

    # -- raw REST calls ----------------------------------------------------
    def _post(self, path: str, body) -> object:
        req = urllib.request.Request(
            self.url + path, data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with self._open(req, timeout=20) as resp:
            return json.loads(resp.read().decode())

    def cmd(self, *args):
        out = self._post("", [str(a) for a in args])
        if isinstance(out, dict) and out.get("error"):
            raise RedisError(out["error"])
        return out.get("result") if isinstance(out, dict) else out

    def pipeline(self, commands: list[list]) -> list:
        if not commands:
            return []
        out = self._post("/pipeline", [[str(a) for a in c] for c in commands])
        results = []
        for item in out:
            if item.get("error"):
                raise RedisError(item["error"])
            results.append(item.get("result"))
        return results

    # -- JSON documents ----------------------------------------------------
    def _get_json(self, key: str) -> dict:
        raw = self.cmd("GET", self.p + key)
        return json.loads(raw) if raw else {}

    def _set_json(self, key: str, obj) -> None:
        self.cmd("SET", self.p + key, json.dumps(obj, ensure_ascii=False, separators=(",", ":")))

    def load_state(self) -> dict:
        return self._get_json("state")

    def save_state(self, state: dict) -> None:
        self._set_json("state", state)

    def load_latest(self) -> dict:
        return self._get_json("latest")

    def save_latest(self, latest: dict) -> None:
        self._set_json("latest", latest)

    # -- history: one list per watch, change points only ---------------------
    def add_history(self, rows: list[dict]) -> None:
        if not rows:
            return
        last = self.cmd("HGETALL", self.p + "lastpoint") or []
        last = dict(zip(last[0::2], last[1::2]))
        commands = []
        for r in rows:
            sig = f"{r.get('price', '')}|{r.get('flights', '')}|{r.get('status', '')}"
            if last.get(r["watch_id"]) == sig:
                continue
            last[r["watch_id"]] = sig
            commands.append(["RPUSH", f"{self.p}hist:{r['watch_id']}",
                             json.dumps({k: r.get(k, "") for k in HISTORY_FIELDS}, separators=(",", ":"))])
            commands.append(["HSET", self.p + "lastpoint", r["watch_id"], sig])
        self.pipeline(commands)

    def load_history(self, watch_ids: list[str] | None = None) -> list[dict]:
        if watch_ids is None:
            keys = self.cmd("KEYS", self.p + "hist:*") or []
        else:
            keys = [f"{self.p}hist:{w}" for w in watch_ids]
        lists = self.pipeline([["LRANGE", k, 0, -1] for k in keys])
        rows = [json.loads(x) for lst in lists for x in (lst or [])]
        return sorted(rows, key=lambda r: r["checked_at"])

    # -- alert log -------------------------------------------------------------
    def add_events(self, rows: list[dict]) -> None:
        if not rows:
            return
        commands = [["RPUSH", self.p + "events", json.dumps(r, separators=(",", ":"))] for r in rows]
        commands.append(["LTRIM", self.p + "events", -MAX_EVENTS, -1])
        self.pipeline(commands)

    def load_events(self) -> list[dict]:
        return [json.loads(x) for x in (self.cmd("LRANGE", self.p + "events", 0, -1) or [])]

    # -- one check at a time -----------------------------------------------------
    def acquire_lock(self, seconds: int) -> bool:
        self._lock_id = uuid.uuid4().hex
        return self.cmd("SET", self.p + "lock", self._lock_id, "NX", "EX", seconds) == "OK"

    def release_lock(self) -> None:
        if self._lock_id and self.cmd("GET", self.p + "lock") == self._lock_id:
            self.cmd("DEL", self.p + "lock")
        self._lock_id = None


def redis_env() -> tuple[str, str] | None:
    """Upstash's own names, or the KV_* names the Vercel integration may use."""
    for url_name, token_name in (("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"),
                                 ("KV_REST_API_URL", "KV_REST_API_TOKEN")):
        if os.environ.get(url_name) and os.environ.get(token_name):
            return os.environ[url_name], os.environ[token_name]
    return None


def open_store(data_dir: Path):
    env = redis_env()
    return RedisStore(*env) if env else FileStore(data_dir)


