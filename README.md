# k_transcriptor

Ferramenta de linha de comando para **buscar vídeos no YouTube, baixar só o áudio e transcrevê-los localmente** com o Whisper, usando a placa de vídeo NVIDIA quando houver. Todo o trabalho passa por uma **fila única em disco**: você pode chamar o comando em vários terminais ao mesmo tempo e todos mostram a **mesma tela**, com o estado de cada vídeo.

Ela existe para quem precisa **ler um vídeo antes de recomendá-lo**. A transcrição, com minutagem, mostra o que o vídeo realmente explica, em que minuto, e se ele erra ou contradiz o material de referência. É o motor de transcrição do projeto [k_study](../k_study) (trilhas de aprendizagem criadas por agentes de IA), mas funciona sozinho e não depende de nada dele.

## O que ela faz

- **Busca** vídeos no YouTube pela página de resultados, sem chave de API.
- **Baixa só o áudio** (m4a, cerca de 1 MB por minuto), **um download por vez**, com pausa entre eles.
- **Transcreve localmente** com o `faster-whisper` (modelo `small` por padrão): na GPU, cerca de 20 vezes mais rápido que o tempo real; na CPU, cerca de 4 vezes o tempo do vídeo.
- **Guarda um cache** de transcrições: vídeo já transcrito nunca é transcrito de novo.
- **Mostra um painel colorido** no terminal com fase, título, barra de progresso e tempo de áudio já transcrito de cada vídeo, igual em todos os terminais.
- **Busca legendas** do próprio YouTube (rápido, mas o YouTube bloqueia o IP depois de uns 15 pedidos).

## Requisitos

- Python 3.10 ou superior.
- Para transcrever na GPU: placa NVIDIA com driver atualizado (as bibliotecas CUDA são instaladas pelo `setup`). Sem GPU, funciona na CPU, mais devagar.
- Conexão com a internet para buscar e baixar; o modelo Whisper (cerca de 0,5 GB) é baixado uma vez.
- Não precisa de `ffmpeg`.

## Instalação

Basta rodar o `main.py` da raiz; **não é preciso instalar o pacote nem mexer no PATH ou em variáveis de ambiente**:

```bash
py main.py setup
```

(no Linux ou macOS, `python3 main.py setup`). O `setup` instala no Python em uso as dependências (`yt-dlp`, `faster-whisper`, `youtube-transcript-api` e, na GPU, `nvidia-cublas-cu12` e `nvidia-cudnn-cu12`), confere a instalação e baixa o modelo. Opções: `--no-gpu` (não instala as bibliotecas NVIDIA) e `--no-model` (não baixa o modelo agora).

Para ter o comando curto `ktr` em qualquer pasta (opcional):

```bash
pip install -e ".[all]"
ktr doctor
```

Dica: use um ambiente virtual (`py -m venv .venv`) se não quiser instalar as dependências no Python do sistema. Se algo falhar, rode `py main.py doctor`: ele diz o que falta.

## Primeiros passos

```bash
py main.py search regressão linear explicação        # id | duração | canal | título | visualizações
py main.py transcribe ID1 ID2 --language auto        # entra na fila e espera terminar
py main.py text ID1                                  # lê a transcrição inteira, com mm:ss
py main.py transcribe ID1 --copy-to ../minha-aula/transcripts   # e copia o resultado para uma pasta
py main.py queue --watch                             # em OUTRO terminal: acompanha a fila ao vivo
py main.py clean                                     # apaga os áudios quando tiver certeza de que não precisa mais deles
```

Dois terminais podem pedir transcrições ao mesmo tempo: os vídeos entram na mesma fila, os downloads continuam um por vez e os dois terminais mostram o mesmo painel.

## Comandos

| Comando | O que faz |
| :-- | :-- |
| `setup [--no-gpu] [--no-model]` | instala as dependências e baixa o modelo |
| `doctor [--download-model]` | confere yt-dlp, faster-whisper e a GPU (e baixa o modelo, se pedido) |
| `search TERMOS... [--limit N]` | busca no YouTube; cada linha é `id \| duração \| canal \| título \| visualizações` |
| `transcribe ID\|URL... [opções]` | coloca na fila e transcreve; só volta quando os **seus** vídeos terminam |
| `captions ID\|URL... [--pause S] [--copy-to PASTA]` | baixa as legendas do YouTube para o cache |
| `text ID\|URL` | imprime uma transcrição do cache, uma fala por linha, com `[mm:ss]` |
| `export ID\|URL... --to PASTA` | copia transcrições do cache para uma pasta |
| `queue [--watch]` | mostra a fila compartilhada (uma vez, ou ao vivo com `--watch`) |
| `clean [--force]` | apaga os áudios; o **único** comando que apaga áudio |
| `where` | mostra onde ficam o cache, as transcrições e a fila |

Opções de `transcribe`:

| Opção | Padrão | Efeito |
| :-- | :-- | :-- |
| `--language pt\|en\|...\|auto` | `pt` | idioma do áudio; use `auto` quando o vídeo puder não estar em português (forçar `pt` num áudio em inglês produz um texto sem sentido) |
| `--model` | `small` | `tiny`, `base`, `small`, `medium`, `large-v3` (maiores são mais precisos e mais lentos) |
| `--device auto\|cpu\|cuda` | `auto` | dispositivo de cálculo; `auto` usa a GPU quando ela funciona |
| `--workers N` | `2` | quantos vídeos transcrever ao mesmo tempo |
| `--downloads N` | `1` | quantos vídeos baixar ao mesmo tempo; **não aumente** (educação com o YouTube e menos risco de bloqueio) |
| `--pause S` | `4` | segundos de espera entre downloads |
| `--overwrite` | desligado | transcreve de novo mesmo que já esteja no cache |
| `--no-wait` | desligado | só enfileira e volta |
| `--copy-to PASTA` | nenhuma | também copia as transcrições prontas para a pasta |

Códigos de saída: `0` ok · `1` algum vídeo falhou · `2` erro de uso ou de ambiente · `3` o YouTube bloqueou os downloads.

## Como funciona por dentro

**Pipeline de um lote** (`transcribe.py`): uma thread baixa o áudio do próximo vídeo para `work/<id>/` enquanto até `--workers` threads transcrevem os já baixados. Uma fila limitada impede acumular áudio demais. O progresso do download vem dos *hooks* do yt-dlp e o da transcrição vem da posição do último trecho reconhecido.

**Fila única** (`jobs.py`): cada chamada de `transcribe` só **adiciona** vídeos à fila (`work/_queue/jobs/<id>.json`) e depois tenta virar o **executor**, criando o arquivo de trava `runner.lock` por criação exclusiva. Quem não consegue a trava espera. O executor renova a trava a cada 5 s (batimento); uma trava com mais de 60 s sem batimento pertence a um executor que caiu e é assumida pela próxima chamada. O executor esvazia a fila inteira, inclusive vídeos que outras chamadas adicionarem enquanto o lote roda. Cada chamada só termina quando os seus próprios vídeos terminam.

**Estado visível** (`view.py`): o estado de cada vídeo fica em `work/_queue/status/<id>.json` e **todos os terminais desenham a mesma fila lendo esses arquivos**. Num terminal interativo o painel é redesenhado no lugar; sem terminal (arquivo de log, agente), sai uma linha por mudança de fase. O título aparece logo ao enfileirar, vindo do oEmbed do YouTube, sem esperar o download. `NO_COLOR=1` desliga as cores; `FORCE_COLOR=1` as mantém fora de um terminal.

**Bloqueio do YouTube**: ao receber 429 ou "confirme que não é robô", o executor para os downloads, grava `work/_queue/BLOCKED`, mostra um aviso vermelho e sai com código 3. Troque de rede (por exemplo, o celular) e rode o comando de novo: a fila retoma de onde parou. Um 403 passageiro é tentado de novo sozinho.

**Nada é apagado automaticamente.** Os áudios ficam em `work/<id>/` até alguém rodar `ktr clean`, que recusa rodar com um executor ativo (sem `--force`) e nunca mexe na fila. Quem usa a ferramenta (uma pessoa ou um agente) só deve rodar `clean` com a **certeza** de que nenhum vídeo baixado ainda pode ser útil.

## Onde ficam os arquivos

`ktr where` mostra. Por padrão `%LOCALAPPDATA%\k_transcriptor` no Windows e `~/.local/share/k_transcriptor` nos outros sistemas; troque com a variável de ambiente `K_TRANSCRIPTOR_HOME`.

```text
transcripts/<id>.json    o cache: um vídeo já transcrito não é transcrito de novo
work/<id>/               o áudio baixado (até o ktr clean)
work/_queue/             a fila compartilhada: jobs/, status/, runner.lock, BLOCKED, runner.log, meta.json
```

## Formato da transcrição

```json
{ "lang": "pt", "generated": true, "duration": 314, "title": "Título do vídeo",
  "source": "transcribed locally with faster-whisper (small, cuda) from the video audio",
  "segments": [[0, "primeira fala"], [12, "segunda fala"]] }
```

`segments` é uma lista de pares `[segundo, texto]`. A transcrição automática erra nomes técnicos, siglas e sotaques: leia como apoio, não como verdade. Os tempos têm precisão de uns 15 segundos nas legendas do YouTube.

## Usar de outro programa

Outro projeto (como o k_study) chama o comando e lê os arquivos:

1. `ktr transcribe ID... --language auto --copy-to <pasta>` coloca na fila, espera e copia os JSON para a pasta de destino.
2. Ou lê direto de `<cache>/transcripts/<id>.json` (`ktr where` informa a pasta).
3. `ktr text ID` entrega o texto pronto para ler, com `[mm:ss]`.

## Solução de problemas

| Sintoma | O que fazer |
| :-- | :-- |
| Aviso vermelho "O YouTube bloqueou os downloads" | trocar de rede (celular) e rodar o comando de novo |
| `disp. cpu` mesmo com placa NVIDIA | atualizar o driver; rodar `py main.py setup` (instala as bibliotecas CUDA) e `py main.py doctor` |
| `AUSENTE yt_dlp` (ou outro módulo) | `py main.py setup` |
| Texto sem sentido numa transcrição | o áudio provavelmente não está em português: refaça com `--language auto --overwrite` |
| A fila parece travada | `ktr queue`: se o executor está parado e há itens pendentes, rode `ktr transcribe` com um deles de novo (a trava vence em 60 s) |
| Mensagem "Há um executor de transcrição ativo" no `clean` | espere terminar, ou use `--force` se tiver certeza de que ele caiu |

## Estrutura do repositório

```text
main.py                  ponto de entrada (py main.py <comando>)
pyproject.toml           empacotamento; extras: captions, transcribe, gpu, all, dev
src/k_transcriptor/
  cli.py                 os comandos
  paths.py               onde ficam o cache e a fila
  search.py              busca no YouTube
  captions.py            legendas do YouTube
  transcribe.py          pipeline de um lote (download + Whisper)
  jobs.py                a fila única em disco
  view.py                o painel do terminal
tests/                   pytest, sem rede e sem GPU
CLAUDE.md                instruções para agentes de IA que trabalham neste código
```

## Testes

```bash
pip install -e ".[dev]"
pytest
```

Não usam rede nem GPU: simulam o download e o reconhecimento e isolam o cache com `K_TRANSCRIPTOR_HOME`.

## Aviso de uso

Os termos do YouTube em geral proíbem baixar vídeos sem autorização do autor. Use a ferramenta para estudo pessoal, com poucos vídeos, cite sempre o link original e nunca compartilhe o áudio baixado.
