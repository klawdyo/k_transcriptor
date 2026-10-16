"""Local transcription (extra: pip install k-transcriptor[transcribe]).

Pipeline: downloader threads fetch ONLY the AUDIO of the next videos into a temporary folder OUTSIDE the data folder,
while transcriber threads run faster-whisper (GPU if available, else CPU) and write <cache>/transcripts/<id>.json. A video's
audio is KEPT in <work folder>/<video id>/ after its transcript is saved: NOTHING is deleted automatically, only
`ktr clean` removes audio (it is the caller's job, e.g. the agent, to run it when done). This module is the pipeline for one batch; jobs.py adds the shared queue on top of it
(many `ktr transcribe` calls, one runner), and that is what the command line uses.

Politeness towards YouTube: each downloader sleeps `pause` seconds between its downloads; a 403 is retried later and a
429 / bot check stops everything (the log says BLOCKED: change network or wait). YouTube's terms generally forbid
downloading videos without the author's permission: use for personal study, few videos, never keep or share the audio.
"""
import glob
import json
import os
import queue
import shutil
import sys
import threading
import time
from collections import deque
from pathlib import Path

from .paths import work_dir
from . import extract_id

QUEUE_NAME = "_queue"          # shared queue folder inside the work folder (see jobs.py); clean_work never touches it

_hooks = threading.local()     # per-thread progress callbacks (set by transcribe_many, read by download_audio / recognize)
_models: dict = {}
_models_lock = threading.Lock()


def log_line(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def add_cuda_dll_dirs() -> None:
    """Windows: make the NVIDIA cuBLAS/cuDNN DLLs (pip packages nvidia-cublas-cu12, nvidia-cudnn-cu12) visible."""
    if not hasattr(os, "add_dll_directory"):
        return
    for entry in list(sys.path):
        for folder in glob.glob(os.path.join(entry or ".", "nvidia", "*", "bin")):
            try:
                os.add_dll_directory(folder)
            except OSError:
                pass
            os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")


def pick_device(device: str = "auto") -> tuple[str, str]:
    """Returns (device, compute_type). 'auto' uses CUDA when CTranslate2 sees a GPU and its libraries load."""
    if device in ("auto", "cuda"):
        add_cuda_dll_dirs()
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda", "float16"
        except Exception:
            pass
        if device == "cuda":
            raise RuntimeError("CUDA pedida, mas não há GPU nem bibliotecas utilizáveis.")
    return "cpu", "int8"


def load_model(model: str, device: str, workers: int):
    from faster_whisper import WhisperModel
    dev, compute = pick_device(device)
    key = (model, dev, compute, workers)
    with _models_lock:
        if key not in _models:
            _models[key] = WhisperModel(model, device=dev, compute_type=compute, num_workers=workers)
        return _models[key]


def download_audio(video_id: str, folder: Path):
    import yt_dlp
    def on_progress(d: dict) -> None:
        callback = getattr(_hooks, "download", None)
        if callback and d.get("status") == "downloading":
            info = d.get("info_dict") or {}
            callback(title=info.get("title"), duration=info.get("duration"), bytes_done=d.get("downloaded_bytes"),
                     bytes_total=d.get("total_bytes") or d.get("total_bytes_estimate"))

    options = {"format": "bestaudio[ext=m4a]/bestaudio", "outtmpl": str(folder / "%(id)s.%(ext)s"),
               "noplaylist": True, "quiet": True, "no_warnings": True, "noprogress": True,
               "progress_hooks": [on_progress]}
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info("https://www.youtube.com/watch?v=" + video_id, download=True)
    files = [f for f in folder.glob(video_id + ".*") if f.suffix not in (".part", ".ytdl")]
    return max(files, key=lambda f: f.stat().st_mtime), info


def recognize(audio: Path, model: str = "small", language: str = "pt", device: str = "auto",
              workers: int = 1) -> tuple[list[list], str]:
    """Returns (segments, language). language="auto" lets Whisper detect it."""
    wanted = None if language in (None, "auto") else language
    segments, info = load_model(model, device, workers).transcribe(
        str(audio), language=wanted, vad_filter=True, beam_size=5)
    callback = getattr(_hooks, "progress", None)
    result = []
    for s in segments:
        result.append([round(s.start), s.text.strip()])
        if callback and getattr(info, "duration", None):
            callback(min(1.0, s.end / info.duration))
    return result, (wanted or info.language)


def clean_work() -> int:
    """Deletes leftover temporary AUDIO folders (never the queue). Only `ktr clean` calls this. Returns how many."""
    root = work_dir()
    removed = 0
    if root.exists():
        for child in root.iterdir():
            if child.name == QUEUE_NAME:
                continue
            shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
            removed += 1
        try:
            root.rmdir()
        except OSError:
            pass
    return removed


def _is_block(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "too many requests" in text or "not a bot" in text


def _is_transient(exc: Exception) -> bool:
    return "403" in str(exc)


def transcribe_many(entries: list[str], out_dir: Path, model: str = "small", language: str = "pt",
                    pause: int = 4, overwrite: bool = False, device: str = "auto",
                    downloads: int = 1, workers: int = 2, log=log_line, on_event=None, more=None) -> dict:
    """Downloads `downloads` videos at a time (default 1: one by one) and transcribes `workers` at a time. Returns a summary dict.

    `on_event(video_id, **fields)` (optional) reports each video's state: phase = downloading | downloaded |
    transcribing | done | failed, plus title, duration, bytes_done/bytes_total, pct (0..1), error, note.

    `more()` (optional) is asked for more video ids whenever the downloader runs out of work, so videos queued while the
    batch runs are downloaded right away (the downloader never sits idle while the queue has something)."""
    def emit(video_id: str, **fields) -> None:
        if on_event:
            on_event(video_id, **fields)

    ids = list(dict.fromkeys(extract_id(e) for e in entries))
    todo = [i for i in ids if overwrite or not (out_dir / f"{i}.json").exists()]
    for skipped in set(ids) - set(todo):
        log(f"{skipped}: já transcrito; ignorado")
    summary = {"ok": [], "failed": [], "blocked": False, "handled": []}
    if not todo:
        return summary
    dev, compute = pick_device(device)
    log(f"{len(todo)} vídeo(s); dispositivo={dev}/{compute}; downloads={downloads}; transcrições simultâneas={workers}; modelo={model}")
    ready: queue.Queue = queue.Queue(maxsize=downloads + workers)
    blocked = threading.Event()
    lock = threading.Lock()
    retry: list[str] = []

    def fetch(video_id: str) -> None:
        if blocked.is_set():
            return
        folder = work_dir() / video_id
        folder.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        _hooks.download = lambda **fields: emit(video_id, phase="downloading", **fields)
        for attempt in (1, 2):
            try:
                emit(video_id, phase="downloading", note=None)
                audio, info = download_audio(video_id, folder)
                log(f"{video_id}: baixado {audio.stat().st_size / 1e6:.1f} MB em {time.time() - t0:.0f} s - {info.get('title')} ({info.get('duration')} s)")
                emit(video_id, phase="downloaded", title=info.get("title"), duration=info.get("duration"),
                     size_mb=round(audio.stat().st_size / 1e6, 1), pct=None, bytes_done=None, bytes_total=None)
                ready.put((video_id, audio, info, folder))
                break
            except Exception as exc:
                if _is_block(exc):
                    blocked.set()
                    log(f"{video_id}: BLOQUEADO pelo YouTube ({str(exc)[:80]}). Troque de rede ou espere; downloads interrompidos.")
                    emit(video_id, phase="queued", note="bloqueado pelo YouTube; volta quando você rodar de novo", pct=None)
                    return
                if attempt == 1 and _is_transient(exc):
                    log(f"{video_id}: erro 403, nova tentativa em 30 s")
                    emit(video_id, note="erro 403 passageiro; nova tentativa em 30 s")
                    time.sleep(30)
                    continue
                with lock:
                    (retry if _is_transient(exc) else summary["failed"]).append(video_id)
                log(f"{video_id}: falha no download ({type(exc).__name__}: {str(exc)[:100]})")
                if not _is_transient(exc):
                    emit(video_id, phase="failed", error=f"download: {str(exc)[:120]}")
                else:
                    emit(video_id, note="erro 403; nova tentativa no fim do lote")
                return
        time.sleep(pause)                                   # be gentle with YouTube between downloads of this thread

    def transcribe_loop() -> None:
        while True:
            item = ready.get()
            if item is None:
                return
            video_id, audio, info, folder = item
            try:
                t1 = time.time()
                emit(video_id, phase="transcribing", pct=0.0, note=None)
                _hooks.progress = lambda fraction, video_id=video_id: emit(video_id, phase="transcribing", pct=fraction)
                segments, detected = recognize(audio, model, language, device, workers)
                out_dir.mkdir(parents=True, exist_ok=True)
                doc = {"lang": detected, "generated": True, "duration": info.get("duration"), "title": info.get("title"),
                       "source": f"transcribed locally with faster-whisper ({model}, {dev}) from the video audio", "segments": segments}
                (out_dir / f"{video_id}.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
                with lock:
                    summary["ok"].append(video_id)
                log(f"{video_id}: transcrito em {time.time() - t1:.0f} s ({len(segments)} trechos, idioma {detected})")
                emit(video_id, phase="done", pct=1.0, lang=detected, segments=len(segments), seconds=round(time.time() - t1))
            except Exception as exc:
                with lock:
                    summary["failed"].append(video_id)
                log(f"{video_id}: falha na transcrição ({type(exc).__name__}: {str(exc)[:100]})")
                emit(video_id, phase="failed", error=f"transcrição: {str(exc)[:120]}")
            finally:
                _hooks.progress = None
                log(f"{video_id}: áudio mantido em {folder} (apague com `ktr clean`)")

    consumers = [threading.Thread(target=transcribe_loop, daemon=True) for _ in range(workers)]
    for t in consumers:
        t.start()
    pending, handed, pull_lock = deque(todo), list(todo), threading.Lock()

    def next_id():
        with pull_lock:
            if blocked.is_set():
                return None
            if not pending and more:
                for extra in more():
                    if extra not in handed:
                        handed.append(extra)
                        pending.append(extra)
            return pending.popleft() if pending else None

    def downloader() -> None:
        while True:
            video_id = next_id()
            if video_id is None:
                return
            fetch(video_id)

    fetchers = [threading.Thread(target=downloader, daemon=True) for _ in range(downloads)]
    for t in fetchers:
        t.start()
    for t in fetchers:
        t.join()
    for video_id in retry:                                  # one more try, sequentially, for the videos that got a transient 403
        if not blocked.is_set():
            log(f"{video_id}: nova tentativa depois do lote")
            time.sleep(20)
            fetch(video_id)
    summary["handled"] = list(handed)
    for _ in consumers:
        ready.put(None)
    for t in consumers:
        t.join()
    summary["blocked"] = blocked.is_set()
    log(f"fim do lote: {len(summary['ok'])} ok, {len(summary['failed'])} com falha, bloqueado={summary['blocked']}")
    return summary
