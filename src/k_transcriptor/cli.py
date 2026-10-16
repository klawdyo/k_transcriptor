"""Linha de comando:  ktr <comando>   (ou: py main.py <comando>, ou: python -m k_transcriptor <comando>)

  ktr setup       [--no-gpu] [--no-model]  instala as dependências (yt-dlp, faster-whisper, GPU) e baixa o modelo
  ktr doctor      [--download-model]       confere a instalação (yt-dlp, faster-whisper, GPU)
  ktr search      TERMOS...                busca no YouTube (id | duração | canal | título | visualizações)
  ktr transcribe  ID|URL...                entra na fila única e transcreve localmente (GPU se houver); espera terminar
  ktr captions    ID|URL...                baixa as legendas do próprio YouTube (rápido, mas o IP é bloqueado após ~15)
  ktr text        ID|URL                   imprime uma transcrição do cache com mm:ss
  ktr export      ID|URL... --to PASTA     copia transcrições do cache para uma pasta
  ktr queue       [--watch]                mostra a fila compartilhada (igual em todos os terminais)
  ktr clean       [--force]                apaga os áudios baixados (o ÚNICO comando que apaga áudio)
  ktr where                                mostra onde fica o cache

Tudo fica em cache sob $K_TRANSCRIPTOR_HOME (veja `ktr where`); `--copy-to PASTA` também copia as transcrições
prontas para PASTA. Códigos de saída: 0 ok, 1 algum vídeo falhou, 2 erro de uso ou de ambiente, 3 o YouTube bloqueou.
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import extract_id, paths

BASE_PACKAGES = ["yt-dlp", "faster-whisper", "youtube-transcript-api"]
GPU_PACKAGES = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _copy(ids: list[str], destination: str) -> list[str]:
    """Copia transcrições do cache para `destination`. Devolve os ids que não estavam no cache."""
    target = Path(destination).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    missing = []
    for video_id in ids:
        source = paths.transcripts_dir() / f"{video_id}.json"
        if source.exists():
            shutil.copy2(source, target / source.name)
        else:
            missing.append(video_id)
    return missing


def _doctor(download_model: bool) -> int:
    from .transcribe import add_cuda_dll_dirs, load_model, pick_device
    problems = 0
    for module, package in (("yt_dlp", "yt-dlp"), ("faster_whisper", "faster-whisper"),
                            ("youtube_transcript_api", "youtube-transcript-api")):
        try:
            __import__(module)
            print(f"ok        {module}")
        except ImportError:
            problems += 1
            print(f"AUSENTE   {module}  -> rode `ktr setup` (ou: pip install {package})")
    add_cuda_dll_dirs()
    try:
        device, compute = pick_device("auto")
    except Exception as exc:
        device, compute = "cpu", f"(erro: {exc})"
    print(f"disp.     {device}/{compute}")
    if device != "cuda":
        print("          sem GPU NVIDIA utilizável: a transcrição usará a CPU (cerca de 4 vezes a duração do vídeo);"
              " para a GPU rode `ktr setup` e confira os drivers da NVIDIA")
    print(f"cache     {paths.home()}")
    if download_model and problems == 0:
        print("carregando o modelo Whisper 'small' (baixa cerca de 0,5 GB no primeiro uso)...")
        load_model("small", "auto", 1)
        print("ok        modelo pronto")
    return 1 if problems else 0


def _setup(no_gpu: bool, no_model: bool) -> int:
    packages = BASE_PACKAGES + ([] if no_gpu else GPU_PACKAGES)
    print(f"instalando com o Python em uso ({sys.executable}): {' '.join(packages)}")
    result = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", *packages])
    if result.returncode != 0:
        print("erro: o pip falhou; veja a mensagem acima.", file=sys.stderr)
        return 2
    return _doctor(download_model=not no_model)


def main(argv=None) -> int:
    _utf8()
    ap = argparse.ArgumentParser(prog="ktr", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True, metavar="comando")

    s = sub.add_parser("setup", help="instala as dependências e baixa o modelo")
    s.add_argument("--no-gpu", action="store_true", help="não instala as bibliotecas CUDA da NVIDIA")
    s.add_argument("--no-model", action="store_true", help="não baixa o modelo Whisper agora")

    s = sub.add_parser("doctor", help="confere a instalação")
    s.add_argument("--download-model", action="store_true", help="também baixa o modelo Whisper")

    s = sub.add_parser("search", help="busca no YouTube")
    s.add_argument("terms", nargs="+", metavar="termos")
    s.add_argument("--limit", type=int, default=12, help="quantidade de resultados (padrão 12)")

    s = sub.add_parser("transcribe", help="transcreve localmente pela fila única")
    s.add_argument("videos", nargs="+", metavar="id_ou_url")
    s.add_argument("--model", default="small", help="tiny, base, small (padrão), medium, large-v3")
    s.add_argument("--language", default="pt", help="pt (padrão), en, ... ou auto (use auto quando o vídeo puder não estar em português)")
    s.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"], help="dispositivo de cálculo (padrão auto)")
    s.add_argument("--downloads", type=int, default=1, help="vídeos baixados ao mesmo tempo (padrão 1, um por vez; não aumente)")
    s.add_argument("--workers", type=int, default=2, help="vídeos transcritos ao mesmo tempo (padrão 2)")
    s.add_argument("--pause", type=int, default=4, help="segundos de espera entre downloads (padrão 4)")
    s.add_argument("--overwrite", action="store_true", help="transcreve de novo mesmo que já esteja no cache")
    s.add_argument("--no-wait", action="store_true", help="só coloca na fila e volta (padrão: espera os vídeos terminarem)")
    s.add_argument("--copy-to", metavar="PASTA", help="também copia as transcrições prontas para PASTA")

    s = sub.add_parser("captions", help="baixa as legendas do próprio YouTube")
    s.add_argument("videos", nargs="+", metavar="id_ou_url")
    s.add_argument("--pause", type=int, default=6, help="segundos entre consultas (padrão 6)")
    s.add_argument("--copy-to", metavar="PASTA", help="também copia as legendas para PASTA")

    s = sub.add_parser("text", help="imprime uma transcrição do cache com mm:ss")
    s.add_argument("video", metavar="id_ou_url")

    s = sub.add_parser("export", help="copia transcrições do cache para uma pasta")
    s.add_argument("videos", nargs="+", metavar="id_ou_url")
    s.add_argument("--to", required=True, metavar="PASTA", help="pasta de destino")

    s = sub.add_parser("queue", help="mostra a fila compartilhada")
    s.add_argument("--watch", action="store_true", help="redesenha ao vivo até Ctrl+C (abra em outro terminal)")

    s = sub.add_parser("clean", help="apaga os áudios baixados")
    s.add_argument("--force", action="store_true", help="mesmo que pareça haver um executor ativo")

    sub.add_parser("where", help="mostra onde fica o cache")

    args = ap.parse_args(argv)
    try:
        if args.command == "setup":
            return _setup(args.no_gpu, args.no_model)

        if args.command == "doctor":
            return _doctor(args.download_model)

        if args.command == "search":
            from .search import search
            for row in search(" ".join(args.terms), args.limit):
                print(" | ".join(row))
            return 0

        if args.command == "where":
            print(f"pasta base   {paths.home()}")
            print(f"transcrições {paths.transcripts_dir()}")
            print(f"trabalho     {paths.work_dir()}  (áudios e a fila _queue)")
            return 0

        if args.command == "clean":
            from .jobs import runner_active
            from .transcribe import clean_work
            if runner_active() and not args.force:
                print("Há um executor de transcrição ativo: o áudio dele seria apagado. Espere terminar ou use --force.")
                return 1
            print(f"{clean_work()} pasta(s) de áudio apagada(s) em {paths.work_dir()}")
            return 0

        if args.command == "queue":
            import time

            from .jobs import snapshot
            from .view import Watcher, enable_windows_ansi, render, use_color
            if args.watch:
                watcher = Watcher(snapshot, []).start()
                try:
                    while True:
                        time.sleep(3600)
                except KeyboardInterrupt:
                    watcher.stop()
                return 0
            color = use_color()
            if color:
                enable_windows_ansi()
            print(chr(10).join(render(snapshot(), set(), None, color)))
            return 0

        if args.command == "text":
            file = paths.transcripts_dir() / f"{extract_id(args.video)}.json"
            if not file.exists():
                print(f"erro: não há transcrição no cache para {args.video} (rode `ktr transcribe`)", file=sys.stderr)
                return 2
            doc = json.loads(file.read_text(encoding="utf-8"))
            print(f"# {doc.get('title') or file.stem} | {doc.get('lang')} | {doc.get('duration')} s | {doc.get('source', '')}")
            for second, text in doc["segments"]:
                print(f"[{second // 60:02d}:{second % 60:02d}] {text}")
            return 0

        if args.command == "export":
            ids = [extract_id(v) for v in args.videos]
            missing = _copy(ids, args.to)
            print(f"{len(ids) - len(missing)} transcrição(ões) copiada(s) para {args.to}"
                  + (f"; não estão no cache: {' '.join(missing)}" if missing else ""))
            return 1 if missing else 0

        if args.command == "captions":
            from .captions import fetch_many
            ids = [extract_id(v) for v in args.videos]
            fetch_many(ids, paths.transcripts_dir(), args.pause)
            if args.copy_to:
                missing = _copy(ids, args.copy_to)
                if missing:
                    print("sem legendas para:", " ".join(missing))
            return 0

        if args.command == "transcribe":
            from .jobs import submit
            result = submit(args.videos, paths.transcripts_dir(), args.model, args.language, args.pause, args.overwrite,
                            args.device, args.downloads, args.workers, wait=not args.no_wait)
            if args.copy_to:
                missing = _copy([extract_id(v) for v in args.videos], args.copy_to)
                if missing:
                    print("não estão no cache (não copiados):", " ".join(missing))
            return 3 if result["blocked"] else (1 if result["failed"] else 0)
    except ValueError as exc:
        print("erro:", exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
