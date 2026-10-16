"""Shared transcription queue: any number of `ktr transcribe` calls, one runner, one view.

Every call only ADDS its videos to one queue on disk (inside the work folder, outside the data folder) and then either
becomes the single runner (lock file with a heartbeat) or waits for the runner that is already working. So calls from any
number of terminals never download or transcribe at the same time, and a video is never done twice. A call returns when
ITS videos are done (`wait=False` returns right after queuing). Nothing here deletes audio: only `ktr clean` does.

Every instance draws the SAME queue, read from the same files (see view.py), so a video added by another terminal
shows up in all of them.

Layout (under <work folder>/_queue/):
  jobs/<id>.json      pending work (what the runner takes)
  status/<id>.json    what to show for each video: phase, title, progress, error (done/failed ones are dropped when a
                      new session starts on an idle queue)
  failed/<id>.txt     marker that the video was given up
  meta.json, runner.lock, runner.log, BLOCKED
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .paths import work_dir
from . import extract_id
from .transcribe import QUEUE_NAME, log_line, pick_device, transcribe_many
from .view import Watcher

LOCK_STALE_SECONDS = 60          # a lock whose heartbeat is older than this belongs to a crashed runner
_status_lock = threading.Lock()


def queue_dir() -> Path:
    return work_dir() / QUEUE_NAME


def _jobs_dir() -> Path:
    return queue_dir() / "jobs"


def _status_dir() -> Path:
    return queue_dir() / "status"


def _failed_dir() -> Path:
    return queue_dir() / "failed"


def _lock_file() -> Path:
    return queue_dir() / "runner.lock"


def _blocked_file() -> Path:
    return queue_dir() / "BLOCKED"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    for attempt in range(5):                                     # Windows: a reader may hold the target for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    _remove(tmp)


def _remove(path: Path) -> None:
    """Unlink a shared file; on Windows another instance may be reading it for a moment, so retry."""
    for attempt in range(10):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.05)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- per-video status (what every instance shows)
def update_status(video_id: str, **fields) -> None:
    with _status_lock:
        path = _status_dir() / f"{video_id}.json"
        status = _read_json(path) or {"id": video_id, "phase": "queued", "added": time.time()}
        for key in ("title", "duration"):                        # never erase what we already know with an empty value
            if fields.get(key) is None and status.get(key) is not None:
                fields.pop(key, None)
        status.update(fields)
        status["updated"] = time.time()
        _write_json(path, status)


def make_on_event():
    """Callback for transcribe_many: writes status files, but at most once a second for pure progress updates."""
    last: dict = {}

    def on_event(video_id: str, **fields) -> None:
        keys = set(fields) - {"pct", "bytes_done", "bytes_total", "title", "duration", "phase"}
        progress_only = not keys and fields.get("phase") in ("downloading", "transcribing")
        now = time.time()
        if progress_only and now - last.get(video_id, 0) < 1.0:
            return
        last[video_id] = now
        update_status(video_id, **fields)

    return on_event


def fetch_title(video_id: str) -> dict:
    """Title and channel from YouTube's public oEmbed endpoint: one tiny request, no download. {} when unavailable."""
    url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(f"https://www.youtube.com/watch?v={video_id}", safe="")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "k-transcriptor"}), timeout=6) as response:
            data = json.loads(response.read().decode("utf-8"))
        return {"title": data.get("title"), "channel": data.get("author_name")}
    except Exception:
        return {}


def fill_titles(ids: list[str]) -> None:
    """Sets the title of queued videos as soon as they are queued (one by one, politely), without waiting for the download."""
    for video_id in ids:
        current = _read_json(_status_dir() / f"{video_id}.json") or {}
        if current.get("title"):
            continue
        found = fetch_title(video_id)
        if found.get("title"):
            update_status(video_id, **found)
        time.sleep(0.3)


def snapshot() -> dict:
    videos = []
    if _status_dir().exists():
        videos = [v for v in (_read_json(f) for f in _status_dir().glob("*.json")) if v]
    return {"videos": videos, "meta": _read_json(queue_dir() / "meta.json"),
            "blocked": _blocked_file().exists(), "runner": runner_active()}


# ---------------------------------------------------------------- runner lock
def runner_active() -> bool:
    try:
        return time.time() - _lock_file().stat().st_mtime < LOCK_STALE_SECONDS
    except OSError:
        return False


def _acquire_runner() -> bool:
    """Exclusive creation of the lock file; a lock whose heartbeat stopped (crashed runner) is taken over."""
    queue_dir().mkdir(parents=True, exist_ok=True)
    lock = _lock_file()
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            if runner_active():
                return False
            try:
                lock.unlink()
            except OSError:
                return False
    return False


def _release_runner() -> None:
    try:
        _lock_file().unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- queue
def enqueue(ids: list[str], model: str, language: str, overwrite: bool) -> int:
    """Adds videos to the queue (an id already queued is not added twice). Clears old failure/block marks of a new request."""
    _jobs_dir().mkdir(parents=True, exist_ok=True)
    _failed_dir().mkdir(parents=True, exist_ok=True)
    _remove(_blocked_file())
    if not runner_active() and not any(_jobs_dir().glob("*.json")) and _status_dir().exists():
        for f in _status_dir().glob("*.json"):                   # idle queue: a new session starts, drop the old finished rows
            status = _read_json(f)
            if status and status.get("phase") in ("done", "failed"):
                _remove(f)
    added = 0
    for video_id in ids:
        _remove(_failed_dir() / f"{video_id}.txt")
        job = _jobs_dir() / f"{video_id}.json"
        if job.exists():
            continue
        _write_json(job, {"id": video_id, "model": model, "language": language, "overwrite": overwrite})
        update_status(video_id, phase="queued", pct=None, error=None, note=None, bytes_done=None, bytes_total=None,
                      added=time.time())
        added += 1
    return added


def _read_jobs() -> list[dict]:
    found = []
    if _jobs_dir().exists():
        for f in _jobs_dir().glob("*.json"):
            try:                                                     # a job may be removed by the runner meanwhile
                found.append((f.stat().st_mtime, json.loads(f.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                continue
    return [job for _, job in sorted(found, key=lambda pair: pair[0])]


def queue_status() -> dict:
    return {"pending": [j["id"] for j in _read_jobs()],
            "failed": sorted(p.stem for p in _failed_dir().glob("*.txt")) if _failed_dir().exists() else [],
            "blocked": _blocked_file().exists(), "runner": runner_active()}


def file_log(message: str) -> None:
    """Default log of the pipeline: appended to <queue>/runner.log (the terminal shows the shared view instead)."""
    queue_dir().mkdir(parents=True, exist_ok=True)
    with open(queue_dir() / "runner.log", "a", encoding="utf-8") as handle:
        handle.write(f"[{time.strftime('%H:%M:%S')}] {message}\n")


def _run_queue(out_dir: Path, pause: int, device: str, downloads: int, workers: int, log) -> None:
    """The runner: drains the queue (including videos other calls add meanwhile) until it is empty or YouTube blocks."""
    stop = threading.Event()

    def heartbeat() -> None:
        while not stop.wait(5):
            try:
                os.utime(_lock_file())
            except OSError:
                pass

    threading.Thread(target=heartbeat, daemon=True).start()
    on_event = make_on_event()
    try:
        while True:
            jobs = []
            for job in _read_jobs():
                if not job.get("overwrite") and (out_dir / f"{job['id']}.json").exists():
                    _remove(_jobs_dir() / f"{job['id']}.json")        # already done
                    update_status(job["id"], phase="done", pct=1.0)
                else:
                    jobs.append(job)
            if not jobs:
                return
            first = jobs[0]
            key = (first["model"], first["language"], bool(first.get("overwrite")))
            ids = [j["id"] for j in jobs if (j["model"], j["language"], bool(j.get("overwrite"))) == key]
            try:
                dev, compute = pick_device(device)
                _write_json(queue_dir() / "meta.json", {"device": f"{dev}/{compute}", "model": key[0], "language": key[1],
                                                         "pid": os.getpid()})
            except Exception:
                pass
            for video_id in ids:                                     # leftovers of a crashed runner start over
                current = _read_json(_status_dir() / f"{video_id}.json") or {}
                if current.get("phase") in ("downloading", "downloaded", "transcribing"):
                    update_status(video_id, phase="queued", pct=None, note=None, bytes_done=None, bytes_total=None)
            log(f"fila: {len(jobs)} pendente(s); executando {len(ids)} (modelo={key[0]}, idioma={key[1]})")
            threading.Thread(target=fill_titles, args=(list(ids),), daemon=True).start()
            seen = set(ids)

            def more() -> list[str]:
                """Videos queued by other calls while this batch runs (same settings): taken right away."""
                extra = []
                for job in _read_jobs():
                    same = (job["model"], job["language"], bool(job.get("overwrite"))) == key
                    if job["id"] in seen or not same:
                        continue
                    if not key[2] and (out_dir / f"{job['id']}.json").exists():
                        continue
                    seen.add(job["id"])
                    extra.append(job["id"])
                if extra:
                    log(f"fila: {len(extra)} vídeo(s) adicionado(s) enquanto o lote rodava")
                    threading.Thread(target=fill_titles, args=(list(extra),), daemon=True).start()
                return extra

            summary = transcribe_many(ids, out_dir, key[0], key[1], pause, key[2], device, downloads, workers, log, on_event, more)
            for video_id in summary["handled"] or ids:
                if video_id in summary["ok"]:
                    _remove(_jobs_dir() / f"{video_id}.json")
                elif not summary["blocked"]:
                    _failed_dir().mkdir(parents=True, exist_ok=True)
                    (_failed_dir() / f"{video_id}.txt").write_text("not transcribed; see runner.log", encoding="utf-8")
                    _remove(_jobs_dir() / f"{video_id}.json")
                    current = _read_json(_status_dir() / f"{video_id}.json") or {}
                    if current.get("phase") != "failed":
                        update_status(video_id, phase="failed", error=current.get("note") or "não foi transcrito (veja runner.log)",
                                      note=None)
            if summary["blocked"]:
                _blocked_file().write_text("blocked by YouTube", encoding="utf-8")
                return
    finally:
        stop.set()
        _release_runner()


def submit(entries: list[str], out_dir: Path, model: str = "small", language: str = "pt", pause: int = 4,
           overwrite: bool = False, device: str = "auto", downloads: int = 1, workers: int = 2,
           wait: bool = True, log=None, show: bool = True) -> dict:
    """Queues the videos and, if wait, returns when THEY are done. Returns {'ok','failed','blocked','queued'}.

    `show` draws the shared queue view in the terminal; `log` receives the pipeline's own lines (default: runner.log)."""
    log = log or file_log
    ids = list(dict.fromkeys(extract_id(e) for e in entries))
    todo = [i for i in ids if overwrite or not (out_dir / f"{i}.json").exists()]
    for skipped in set(ids) - set(todo):
        if show:
            log_line(f"{skipped}: já transcrito; ignorado")
    result = {"ok": [], "failed": [], "blocked": False, "queued": 0}
    if not todo:
        return result
    result["queued"] = enqueue(todo, model, language, overwrite)
    threading.Thread(target=fill_titles, args=(list(todo),), daemon=True).start()
    watcher = Watcher(snapshot, todo).start() if show else None
    try:
        while True:
            if _acquire_runner():
                _run_queue(out_dir, pause, device, downloads, workers, log)
            elif not wait:
                return result
            status = queue_status()
            pending = [i for i in todo if i in status["pending"]]
            if not pending:
                break
            if status["blocked"]:
                result["blocked"] = True
                break
            time.sleep(2)
    finally:
        if watcher:
            watcher.stop()
    for video_id in todo:
        if (out_dir / f"{video_id}.json").exists():
            result["ok"].append(video_id)
        elif not result["blocked"]:
            result["failed"].append(video_id)
    return result
