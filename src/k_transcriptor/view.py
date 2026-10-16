"""Terminal view of the shared transcription queue (all instances draw the same thing from the same files).

Interactive terminal: a colored dashboard redrawn in place about once a second.
Pipe / log file (no TTY): one plain line whenever a video changes phase (and every 25 % of a transcription).
Colors are off when NO_COLOR is set or the output is not a terminal (set FORCE_COLOR=1 to keep them).
"""
import os
import shutil
import sys
import threading
import time

PHASES = {                       # phase: (label, ANSI style)
    "queued": ("NA FILA", "30;47"),
    "downloading": ("BAIXANDO", "30;46"),
    "downloaded": ("ÁUDIO PRONTO", "30;43"),
    "transcribing": ("TRANSCREVENDO", "30;45"),
    "done": ("CONCLUÍDO", "30;42"),
    "failed": ("FALHOU", "97;41"),
}
ORDER = ["transcribing", "downloaded", "downloading", "queued", "done", "failed"]


def use_color(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    return bool(os.environ.get("FORCE_COLOR")) or (hasattr(stream, "isatty") and stream.isatty())


def enable_windows_ansi() -> None:
    if os.name == "nt":
        os.system("")                                   # turns on virtual-terminal processing in the console


def paint(text: str, style: str, color: bool) -> str:
    return f"\x1b[{style}m{text}\x1b[0m" if color else text


def clock(seconds) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 3600:d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else f"{seconds // 60:02d}:{seconds % 60:02d}"


def bar(fraction, width: int, color: bool) -> str:
    fraction = max(0.0, min(1.0, fraction or 0.0))
    full = int(round(fraction * width))
    return paint("█" * full, "32", color) + paint("░" * (width - full), "90", color)


def detail(video: dict) -> str:
    phase = video.get("phase")
    if phase == "downloading" and video.get("bytes_total"):
        return f"{video.get('bytes_done', 0) / 1e6:.1f}/{video['bytes_total'] / 1e6:.1f} MB"
    if phase == "downloaded":
        return f"{video.get('size_mb', '?')} MB, esperando vaga para transcrever"
    if phase == "transcribing":
        duration = video.get("duration")
        return f"{clock((video.get('pct') or 0) * duration)}/{clock(duration)} de áudio" if duration else ""
    if phase == "done":
        return f"{video.get('segments', '?')} trechos em {clock(video.get('seconds'))}"
    if phase == "failed":
        return video.get("error") or ""
    return ""


def render(snapshot: dict, mine: set, started: float, color: bool, width: int | None = None) -> list[str]:
    width = width or shutil.get_terminal_size((100, 24)).columns
    videos = sorted(snapshot["videos"], key=lambda v: (ORDER.index(v.get("phase", "queued")), v.get("added", 0)))
    counts = {phase: sum(1 for v in videos if v.get("phase") == phase) for phase in PHASES}
    meta = snapshot.get("meta") or {}
    runner = "ativo" if snapshot["runner"] else "parado"
    lines = [paint(" FILA DE TRANSCRIÇÃO ", "1;97;44", color) + f"  executor {paint(runner, '1;32' if snapshot['runner'] else '1;31', color)}"
             + (f" · {meta.get('device', '?')} · modelo {meta.get('model', '?')}" if meta else "")
             + (f" · decorrido {clock(time.time() - started)}" if started else "")]
    summary = [paint(f" {PHASES[p][0]} {counts[p]} ", PHASES[p][1], color) for p in ("queued", "downloading", "downloaded", "transcribing", "done", "failed")]
    lines.append(" ".join(summary) + f"  total {len(videos)}")
    if snapshot.get("blocked"):
        lines.append(paint(" O YOUTUBE BLOQUEOU OS DOWNLOADS: troque de rede (ex.: celular) e rode `ktr transcribe` de novo. ", "1;97;41", color))
    for v in videos:
        phase = v.get("phase", "queued")
        label, style = PHASES.get(phase, (phase, "0"))
        pct = v.get("pct")
        if phase == "downloading" and v.get("bytes_total"):
            pct = v.get("bytes_done", 0) / v["bytes_total"]
        shown = ""
        if phase in ("downloading", "transcribing", "done"):
            shown = f"{bar(pct, 12, color)} {int(round((pct or 0) * 100)):3d}%"
        else:
            shown = " " * 17
        mark = paint("★", "93", color) if v["id"] in mine else " "
        head = f"{mark} {v['id']:<11} {paint(f' {label:<13} ', style, color)} {shown}  "
        text = (v.get("title") or "(título ainda desconhecido)")
        extra = detail(v)
        if v.get("note"):
            extra = (extra + " · " if extra else "") + v["note"]
        room = max(20, width - 50)                              # keep each row on ONE line (the redraw counts lines)
        extra = extra[:max(10, room // 2)] if extra else ""
        text = text[:max(10, room - len(extra) - 2)]
        lines.append(head + text + (paint(f"  {extra}", "90", color) if extra else ""))
    lines.append(paint("★ = vídeos desta chamada · o executor processa a fila inteira, inclusive o que outras chamadas adicionarem", "90", color))
    return lines


class Watcher:
    """Draws the shared state until stopped. `read` returns the snapshot dict; `mine` are this call's video ids."""

    def __init__(self, read, mine, out=None, interval: float = 1.0):
        self.read, self.mine, self.out = read, set(mine), out or sys.stdout
        self.interval, self.stop_event, self.started = interval, threading.Event(), time.time()
        self.color, self.drawn, self.last = use_color(self.out), 0, {}
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.interactive = hasattr(self.out, "isatty") and self.out.isatty()

    def start(self) -> "Watcher":
        if self.color:
            enable_windows_ansi()
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)
        self._draw()

    def add(self, ids) -> None:
        self.mine.update(ids)

    def _loop(self) -> None:
        while not self.stop_event.wait(self.interval):
            self._draw()

    def _draw(self) -> None:
        try:
            snapshot = self.read()
        except Exception:
            return
        if self.interactive:
            lines = render(snapshot, self.mine, self.started, self.color)
            prefix = f"\x1b[{self.drawn}F\x1b[J" if self.drawn else ""
            self.out.write(prefix + "\n".join(lines) + "\n")
            self.out.flush()
            self.drawn = len(lines)
        else:
            self._print_changes(snapshot)

    def _print_changes(self, snapshot: dict) -> None:
        for v in snapshot["videos"]:
            phase, pct = v.get("phase", "queued"), v.get("pct") or 0
            step = int(pct * 4) if phase == "transcribing" else 0
            key = (phase, step, v.get("note"))
            if self.last.get(v["id"]) == key:
                continue
            self.last[v["id"]] = key
            label = PHASES.get(phase, (phase,))[0]
            extra = f" {int(pct * 100)}%" if phase == "transcribing" else ""
            note = f" ({v['note']})" if v.get("note") else ""
            error = f" [{v['error']}]" if phase == "failed" and v.get("error") else ""
            mark = "*" if v["id"] in self.mine else " "
            self.out.write(f"[{time.strftime('%H:%M:%S')}] {mark} {v['id']} {label}{extra} {v.get('title') or ''}{note}{error}\n")
        self.out.flush()
