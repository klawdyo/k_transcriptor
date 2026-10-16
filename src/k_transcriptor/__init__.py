"""k_transcriptor: busca no YouTube, baixa o áudio e transcreve localmente (Whisper), com uma fila compartilhada."""
import re

try:                                    # a versão vem do pyproject.toml (uma única fonte da verdade)
    from importlib.metadata import version as _version
    __version__ = _version("k-transcriptor")
except Exception:                       # rodando por `py main.py`, sem instalar
    __version__ = "desconhecida"

VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|embed/)([\w-]{11})")


def extract_id(text: str) -> str:
    """Accepts an 11-character video id or a YouTube URL."""
    m = VIDEO_ID.search(text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[\w-]{11}", text):
        return text
    raise ValueError(f"não é um vídeo do YouTube: {text}")
