"""Ponto de entrada na raiz do projeto: `py main.py <comando>` (equivale a `ktr <comando>`).

Funciona sem instalar o pacote e sem o PATH do Python: basta ter as dependências instaladas no Python em uso
(`py main.py setup` instala tudo). Exemplos: `py main.py setup`, `py main.py search regressão linear`,
`py main.py transcribe ID --language auto`, `py main.py queue --watch`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from k_transcriptor.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
