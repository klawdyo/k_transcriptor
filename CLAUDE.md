# k_transcriptor — instruções para agentes

Este projeto faz **três coisas e só elas**: buscar vídeos no YouTube, baixar o áudio e transcrever localmente (Whisper, na GPU se houver). Ele não sabe nada sobre aulas, trilhas, blocos ou qualquer outro projeto que o use. Quem o usa (por exemplo, um servidor de trilhas de aprendizagem) chama o comando `ktr` e recebe arquivos JSON de transcrição.

Idioma: **em inglês** os nomes de arquivos de código, variáveis, funções, classes, constantes, comandos e opções do CLI; **em português do Brasil** o README, este arquivo, as mensagens vistas por pessoas (saída, erros, `--help`, painel, logs de operação), os comentários e as docstrings. Commits: atômicos e mínimos, em Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`, `build:`; `!` ou `BREAKING CHANGE:` para quebra). A versão segue SemVer e é calculada pelos commits (`feat` sobe o minor, `fix` o patch, quebra o major): não edite a versão do `pyproject.toml` à mão. Não faça push por conta própria.

## Comandos (resumo; `ktr --help` é a fonte atual)

| Comando | O que faz |
| :-- | :-- |
| `ktr search TERMOS...` | busca no YouTube: `id \| duração \| canal \| título \| views` |
| `ktr transcribe ID\|URL... [--language pt\|en\|auto] [--copy-to DIR] [--no-wait]` | coloca na fila única e transcreve; **só retorna quando os seus vídeos terminam** |
| `ktr text ID` | imprime a transcrição do cache com `mm:ss` (é assim que se lê um vídeo para conferi-lo) |
| `ktr export ID... --to DIR` | copia transcrições do cache para uma pasta |
| `ktr captions ID...` | legendas do próprio YouTube (rápido, mas o IP é bloqueado após ~15 consultas) |
| `ktr queue [--watch]` | mostra a fila compartilhada; igual em todos os terminais |
| `ktr clean [--force]` | **único** comando que apaga áudio |
| `ktr doctor [--download-model]` | confere a instalação (yt-dlp, faster-whisper, GPU) e baixa o modelo |
| `ktr where` | mostra onde fica o cache |
| `ktr setup [--no-gpu] [--no-model]` | instala as dependências e baixa o modelo (equivale ao antigo instalador) |

Execução e instalação: `py main.py <comando>` na raiz funciona sem instalar o pacote nem mexer no PATH; `py main.py setup` instala as dependências (yt-dlp, faster-whisper, GPU) e baixa o modelo. Opcional: `pip install -e ".[all]"` cria o comando `ktr`. O núcleo usa só a biblioteca padrão; as dependências pesadas são extras (`captions`, `transcribe`, `gpu`).

## Arquitetura

`src/k_transcriptor/`:
- `paths.py` — onde ficam os arquivos (`K_TRANSCRIPTOR_HOME`): `transcripts/` (o cache), `work/<id>/` (áudio) e `work/_queue/` (fila).
- `search.py` — busca pela página de resultados, sem chave de API.
- `captions.py` — legendas do YouTube.
- `transcribe.py` — o pipeline de UM lote: baixa (um por vez) e transcreve (até `--workers` ao mesmo tempo) com faster-whisper; `pick_device` usa CUDA quando existe.
- `jobs.py` — a **fila única em disco**: cada chamada só adiciona vídeos e vira o executor (arquivo de trava com batimento) ou espera o executor que já existe. Estado por vídeo em `_queue/status/`, pendentes em `_queue/jobs/`, bloqueio em `_queue/BLOCKED`.
- `view.py` — o painel colorido do terminal; todas as instâncias desenham a mesma fila lendo os mesmos arquivos. Sem TTY, imprime uma linha por mudança de fase.
- `cli.py` — o comando `ktr` (e `main.py` na raiz, que o chama sem precisar instalar).

Formato da transcrição (`<cache>/transcripts/<id>.json`): `{"lang", "generated", "duration", "title", "source", "segments": [[segundo, "texto"], ...]}`.

## Regras que não se quebram

1. **Nada é apagado automaticamente.** O áudio de cada vídeo fica em `work/<id>/` até alguém rodar `ktr clean`. O código nunca limpa a pasta de trabalho sozinho (nem no começo, nem no fim de um lote). `ktr clean` recusa rodar com executor ativo (sem `--force`) e nunca mexe em `_queue`. Quem chama o `ktr` (o agente) só deve rodar `clean` com **certeza** de que nenhum vídeo baixado pode ser útil.
2. **Um download por vez** (`--downloads 1` é o padrão e não deve ser aumentado): é educação com o YouTube e evita bloqueio. Só a transcrição é paralela. Chamadas simultâneas nunca baixam ao mesmo tempo, porque entram na mesma fila.
3. **Bloqueio do YouTube** (429 ou "confirme que não é robô"): para tudo, grava `_queue/BLOCKED`, mostra o aviso e sai com código 3; a próxima chamada, depois de trocar de rede (por exemplo, celular), limpa o aviso e retoma.
4. **Idioma:** use `--language auto` para vídeos que podem não estar em português; forçar `pt` num áudio em inglês produz um texto sem sentido. Títulos de busca podem vir traduzidos pelo YouTube: confirme o idioma lendo a transcrição.
5. **Uso e termos:** os termos do YouTube em geral proíbem baixar vídeos sem autorização do autor. O projeto serve a estudo pessoal, com poucos vídeos; nunca compartilhe áudio nem transcrições de terceiros.
6. **Escrita atômica** de arquivos compartilhados e novas tentativas no Windows (arquivo em uso por outro processo): mantenha o padrão de `jobs.py` (`_write_json`, `_remove`).
7. Códigos de saída: 0 ok, 1 algum vídeo falhou, 2 erro de uso/ambiente, 3 YouTube bloqueou.

## Como testar

```bash
pip install -e ".[dev]"
pytest
```

Os testes não usam rede nem GPU: simulam o download e o reconhecimento (`monkeypatch` em `transcribe.download_audio` e `transcribe.recognize`) e isolam o cache com `K_TRANSCRIPTOR_HOME` (fixture em `tests/conftest.py`). Teste novo para todo comportamento novo da fila, do painel ou do pipeline; rode a suíte várias vezes quando mexer em concorrência.

## Como outro projeto ou agente usa este

1. Clonar o repositório e rodar `py main.py setup` (instala as dependências e baixa o modelo; `py main.py doctor` confere).
2. `ktr search "tópico"`; escolher candidatos por duração, canal e título.
3. `ktr transcribe ID1 ID2 ... --language auto --copy-to <pasta-de-transcrições-da-aula>`.
4. Ler cada transcrição com `ktr text ID` (inteira, por minuto) para decidir o que aproveita e onde ficam os trechos; nunca decidir pelo título.
5. `ktr queue` para ver o estado; `ktr clean` só no fim e só com certeza.
