# Laboratório de observabilidade em microserviços

Este projeto é um mini sistema funcional para o artigo sobre **observabilidade**. A proposta é provocar falhas em microserviços, acompanhar seus efeitos por logs e métricas e comparar o comportamento antes e depois de uma correção. Os dois cenários iniciais são **tempestade de retentativas** e **falha em cascata**, descritos em [Principais causas de falhas](Pesquisa/Principais%20causas%20de%20falhas.pdf).

## Sistema emulado: loja de roupas

O laboratório também emula uma **loja de roupas**. O cliente escolhe uma peça, o tamanho (`P`, `M` ou `G`) e a quantidade. Um pedido percorre três serviços: `normal` oferece o catálogo e coordena o pedido, `instavel` reserva o estoque da peça no tamanho escolhido e `carga` autoriza um pagamento fictício. O pagamento não movimenta dinheiro. As falhas de estoque e pagamento afetam pedidos dentro da simulação, produzem os mesmos eventos observados pelo agente e podem ser reparadas por ele.

```text
cliente ── POST /orders ──► normal (pedidos e catálogo)
                              ├──► instavel (reserva de estoque)
                              └──► carga (pagamento simulado)
                                      │
                eventos JSON ◄────────┘
                     │
                     ▼
                agente → diagnóstico → reparo → verificação
```

O fluxo é: **escolha da roupa e do tamanho → criação do pedido → reserva do estoque por tamanho → autorização fictícia do pagamento → confirmação**. Se o pagamento retornar erro, o serviço de pedidos tenta liberar a reserva; uma falha nessa compensação aparece como `compensation_pending`. Se uma resposta de rede se perder, o pedido fica em `pending_reconciliation`, pois não é seguro presumir se a operação ocorreu. Cada pedido possui um ID e propaga `X-Request-ID` para correlacionar os eventos. O estado da loja fica em memória e reinicia com os contêineres; o SQLite do agente persiste eventos e decisões. A simulação termina na confirmação; ainda não inclui separação, transporte ou entrega da roupa.

Após subir os serviços, veja o catálogo e envie um pedido:

```powershell
Invoke-RestMethod http://127.0.0.1:8001/products
Invoke-RestMethod http://127.0.0.1:8002/inventory
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8001/orders -ContentType application/json -Body '{"sku":"camiseta-basica","size":"M","quantity":1}'
```

Para preparar os cinco contêineres, executar automaticamente os pedidos e gerar o relatório em **um comando**, use:

```powershell
python containers/executar_loja.py
```

O script cria 22 pedidos durante as falhas, aguarda o agente e envia mais três pedidos para conferir o resultado. Ele salva os dados em `containers/resultados/loja_*.json` e gera um **relatório visual HTML** com resumo, sequência dos pedidos, narrativa do incidente, ações do agente e explicação de cada resultado. Abra o arquivo `.html` indicado no terminal em um navegador de PC. Use `--sem-build` para reutilizar as imagens Docker; `--strategy rf|memory|qwen_memory` e `--cause overload|dependency_timeout` permitem comparar decisões. Se os contêineres já estiverem ativos, `python containers/demo_loja.py` executa somente o episódio. Pedidos individuais podem retornar 503 enquanto a falha estiver ativa; após um reparo verificado, novas tentativas podem ser confirmadas. Para consultar um pedido, use `GET http://127.0.0.1:8001/orders/{order_id}`.

Veja um [exemplo de relatório da loja de roupas](docs/exemplos/loja_roupas.html), acompanhado dos [dados da execução](docs/exemplos/loja_roupas.json). Os relatórios de novas execuções são temporários e ficam em `containers/resultados/`.

Para transformar um JSON anterior em relatório visual:

```powershell
$json = Get-ChildItem containers/resultados/loja_*.json | Sort-Object LastWriteTime -Descending | Select-Object -First 1
python containers/relatorio_loja.py $json.FullName
```

Agora há também um **agente com modelo local**: ele acompanha os logs de três contêineres, classifica padrões de falha, desativa a falha simulada no próprio serviço e verifica se esse serviço voltou a responder. O projeto mantém os experimentos anteriores de retentativas e cascata.

## Demonstração do agente

| Contêiner | Comportamento em `/work` | Padrão que o agente observa |
| --- | --- | --- |
| `normal` | Sempre responde HTTP 200. | Controle saudável. |
| `instavel` | Com a falha ativa, alterna entre HTTP 200 e 503. Após o reparo, responde 200. | Erro intermitente. |
| `carga` | Com a falha ativa, responde HTTP 503. Após o reparo, responde 200. A rota `/carga` original continua disponível. | Erro contínuo. |

O fluxo é: **requisição → serviço → log JSON → coleta em SQLite → modelo local → decisão → reparo da falha simulada → chamada de verificação no mesmo serviço**. O agente consulta `/events` dos três serviços a cada segundo. Ele usa somente horário, serviço, evento, ID de requisição e status; a verdade do experimento fica em um arquivo separado, fora do contêiner do agente.

O detector de anomalia é uma *Random Forest* treinada em sequências sintéticas de respostas normais, intermitentes e contínuas. Ela exige pelo menos seis respostas recentes antes de decidir. O log inclui `error_name` (por exemplo, `OverloadError` ou `DependencyTimeoutError`), preservado no SQLite. O nome do erro é uma pista observada; ele não é a verdade-terreno escondida da avaliação.

Depois da detecção, é possível escolher três estratégias de reparo:

| Estratégia | Decisão de reparo |
| --- | --- |
| `rf` | Escolhe a ação padrão do serviço a partir do padrão de status. É a referência sem memória. |
| `memory` | Reutiliza uma ação para o mesmo serviço e nome de erro após **três reparos verificados**; sem caso suficiente, usa a ação padrão. |
| `qwen_memory` | Usa a mesma memória. Para erro ainda desconhecido, consulta o Qwen2.5 1.5B local com logs resumidos e código-fonte montado somente para leitura. Aceita apenas uma ação da lista permitida; se o modelo estiver indisponível ou responder algo inválido, registra `no_action`. |

No serviço `carga`, `OverloadError` e `DependencyTimeoutError` retornam o **mesmo HTTP 503**, mas exigem `reset_overload` e `restore_dependency`, respectivamente. Uma ação errada mantém a falha ativa. O agente confirma a recuperação no próprio serviço antes de marcar o reparo como verificado e só então aumenta a contagem na memória. As ações usam APIs protegidas por token. Esta é uma prova de conceito com falhas injetadas, não um agente capaz de corrigir bugs arbitrários.

### Executar o ciclo completo

Com Docker Desktop em execução, abra um PowerShell na raiz do projeto e use **um comando**:

```powershell
python containers/executar_cenarios.py
```

Escolha o método e a causa alternativa com `--strategy rf|memory|qwen_memory` e `--cause overload|dependency_timeout`. Para o Qwen local, instale o Ollama e baixe `qwen2.5:1.5b-instruct-q4_K_M` com `ollama pull qwen2.5:1.5b-instruct-q4_K_M`. O arquivo quantizado ocupa cerca de 986 MB; o uso de RAM durante a inferência é maior. O Ollama precisa estar acessível ao contêiner pelo endereço configurado em `OLLAMA_URL`.

Para preparar três incidentes conhecidos e medir os três métodos nas duas causas de HTTP 503:

```powershell
python containers/comparar_detectores.py --sem-build
```

A comparação limpa a **memória de incidentes** antes de começar (mantém o histórico bruto de logs), faz três reparos conhecidos de sobrecarga e então executa cada método em `overload` e `dependency_timeout`. O relatório em `containers/resultados/comparacao_*.json` registra o nome do erro, a origem da decisão, a ação escolhida e a verificação. Sem Ollama, use `--sem-qwen`; o terceiro método não é simulado. Este conjunto pequeno serve para demonstrar o mecanismo, não para estimar generalização.

Para ver o aprendizado de um erro novo: `python containers/demonstrar_aprendizado.py`. O script limpa a memória, deixa Qwen resolver três incidentes `DependencyTimeoutError` e depois muda para `memory`. O último incidente deve ser reparado pela memória sem chamada ao modelo, como mostra o [relatório desta execução](containers/resultados/aprendizado_20261027_202306.json). Os **pesos do Qwen não são alterados**; o que aprende é o banco de casos verificados.

O comando prepara o segredo local, sobe os cinco contêineres necessários, espera as APIs responderem e executa `normal`, `instavel` e `carga`. O terminal apresenta uma tabela com respostas antes e depois do reparo, diagnóstico e verificação; abaixo mostra as ações do agente e logs que serviram de evidência. O relatório completo e a verdade-terreno separada são salvos em `containers/resultados/`. Para reutilizar as imagens Docker já construídas, use `python containers/executar_cenarios.py --sem-build`.

A execução envia 8 chamadas a `normal` e 22 a cada serviço com falha. Depois que o agente age, envia mais 5 chamadas a `instavel` e 5 a `carga`; por isso a tabela mostra 27 chamadas em cada um desses cenários e sempre inclui uma amostra visível do comportamento após o reparo.

Se preferir executar cada etapa manualmente:

```powershell
cd containers
python preparar_ambiente.py
docker compose up --build -d normal instavel carga protecao agente
python demo_agente.py
```

Abra `http://127.0.0.1:8004/dashboard` para ver os eventos, as métricas e as decisões do agente. `http://127.0.0.1:8005/status` mostra o estado do modelo e as decisões em JSON. O comando `python demo_agente.py` salva a verdade-terreno em `containers/resultados/`, sem entregá-la ao agente.

Para repetir e medir vários episódios:

```powershell
python avaliar_agente.py --episodes 3
```

O relatório em `containers/resultados/` inclui recall de detecção, acerto da causa raiz, taxa de reparo verificado, falsos alarmes no controle saudável e tempo médio até a decisão. Cada episódio reativa as falhas simuladas para uma nova comparação, preservando no SQLite os logs e decisões anteriores. Para acompanhar as ações em tempo real:

```powershell
docker compose logs -f --tail=20 agente protecao normal instavel carga
```

O token de controle é gerado em `containers/.env`, não é versionado e não aparece nos logs. A API de controle recusa chamadas sem ele; veja as [decisões de segurança](docs/decisoes-seguranca.md).

**Interpretação da avaliação:** `root_cause_accuracy` exige acertar o serviço, o padrão (`intermittent` ou `continuous`) e a ação adequada à causa ativa. Na [comparação local](containers/resultados/comparacao_20261027_202203.json), `rf` e `memory` repararam 1/2 causas 503 cada; `qwen_memory` reparou 2/2. A memória havia sido preparada somente com três casos de `OverloadError`, por isso não conhecia `DependencyTimeoutError`. O código entregue ao Qwen contém as ações permitidas e a relação entre falha e reparo, portanto esse teste verifica a capacidade de consultar pistas explícitas, não descoberta autônoma de uma causa desconhecida. O conjunto é muito pequeno para estimar acurácia real; uma conclusão científica exige mais incidentes, causas concorrentes, separação de treinamento e teste e intervalos de incerteza.

`docker compose down` encerra os serviços e mantém o volume SQLite. `docker compose down -v` também apaga esse volume e, portanto, os logs e decisões armazenados nele.

## Intenção do projeto

O objetivo principal é **tornar a falha visível e explicável**. Cada chamada recebe um ID de correlação; os serviços registram eventos JSON com esse ID, o cenário, a tentativa e o resultado. O painel mostra os contadores e permite seguir uma requisição entre os serviços. Assim, a correção pode ser avaliada com evidência observável.

Na primeira comparação, a correção é um limite de tentativas com *circuit breaker*. Ela não conserta o serviço instável: durante a falha, algumas requisições ainda recebem HTTP 503. Ela reduz a amplificação do tráfego e permite uma tentativa controlada de recuperação.

Para retentativas, o protótipo compara 100 requisições lógicas em três cenários:

1. **Sem proteção:** cada requisição tenta acessar o serviço instável até quatro vezes.
2. **Apenas limite de retentativas:** cada requisição tenta acessar o serviço até duas vezes, sem *circuit breaker*.
3. **Com proteção:** um intermediário limita cada requisição a duas tentativas, usa um pequeno atraso com variação aleatória entre elas e abre um *circuit breaker* após cinco requisições lógicas falhas. Enquanto o circuito está aberto, devolve erro imediatamente sem chamar o serviço instável. Após cinco segundos, permite uma chamada de prova; se funcionar, fecha o circuito.

## Componentes e funcionamento

```text
experimento.py ── sem proteção ────────────────────────► instavel:8000
               ├─ duas tentativas ─────────────────────► instavel:8000
               └─ com proteção ─► protecao:8000 ──────► instavel:8000

experimento_cascata.py ──► normal:8000 ──► instavel:8000
                                 └──── rota independente
```

- `containers/instavel/main.py`: simula o serviço chamado. `/work?fail=true` sempre retorna HTTP 503; `/work?fail=false` retorna HTTP 200. `/stats` conta as chamadas recebidas.
- `containers/normal/main.py`: no experimento de cascata, chama uma dependência lenta e oferece uma rota independente. Simula capacidade compartilhada e capacidade isolada.
- `containers/protecao/main.py` e `service.py`: formam o intermediário. Ele aplica limite de tentativas, timeout, atraso curto com variação aleatória e *circuit breaker*. `/stats` mostra os contadores e o estado do circuito.
- `containers/experimento.py`: executa os três cenários, lê contadores antes e depois de cada um e imprime o resultado em JSON.
- `containers/experimento_cascata.py`: gera chamadas dependentes concorrentes e mede quantas chamadas independentes continuam funcionando com e sem isolamento.
- `containers/protecao/dashboard.html`: painel de observabilidade em `http://127.0.0.1:8004/dashboard`, atualizado a cada dois segundos.
- `containers/docker-compose.yml`: inicia os serviços. O contêiner `carga` participa da demonstração do agente e mantém a rota `/carga` original.

O circuito começa **fechado**, permitindo chamadas. Após cinco requisições lógicas consecutivas falharem, fica **aberto** e barra novas chamadas ao serviço instável. Cinco segundos depois, fica **meio aberto** e permite uma única chamada de prova. Se ela funcionar, fecha; se falhar, abre novamente.

### Falha em cascata e isolamento

O serviço `normal` tem capacidade compartilhada para quatro operações. No modo `unsafe`, quatro chamadas à dependência lenta ocupam toda essa capacidade por 1,5 segundo. Chamadas à rota independente passam a receber HTTP 503 mesmo sem usar a dependência: a lentidão se propagou.

No modo `protected`, a dependência usa um *bulkhead* próprio com duas vagas. O excedente de chamadas dependentes é rejeitado rapidamente, enquanto a rota independente preserva a capacidade compartilhada. O experimento mede sucessos e rejeições dessa rota em cada modo.

Os valores podem ser alterados pelas variáveis `MAX_ATTEMPTS`, `FAILURE_THRESHOLD`, `COOLDOWN_SECONDS` e `UPSTREAM_TIMEOUT_SECONDS` do serviço `protecao`.

### O que é observado

- **Logs estruturados:** cada evento é escrito como JSON na saída do contêiner, com horário UTC, serviço, cenário, ID da requisição, tipo de evento e status ou número da tentativa.
- **Métricas:** `/stats` mostra chamadas recebidas pelo serviço instável, tentativas feitas pela proteção, requisições barradas, estado do circuito e sucessos/rejeições da rota independente.
- **Correlação:** o cabeçalho `X-Request-ID` acompanha as chamadas entre os serviços. `/events?request_id=...` e o filtro do painel mostram a sequência de eventos de uma requisição.

O painel agrega os dados dos três serviços em `/observability`. Trata-se de correlação por logs, não de um sistema completo de *distributed tracing*.

## Executar com Docker Compose

Pré-requisitos: Docker Desktop em execução e Python 3.10 ou superior instalado para rodar o experimento. Abra um PowerShell na raiz do projeto:

```powershell
cd containers
python preparar_ambiente.py
docker compose up --build -d instavel normal carga protecao agente
```

Abra `http://127.0.0.1:8004/dashboard` no navegador. Para acompanhar os logs JSON em tempo real, deixe um segundo terminal aberto na pasta `containers`:

```powershell
docker compose logs -f --tail=20 instavel normal protecao
```

Em um terceiro terminal, na mesma pasta, execute os experimentos:

```powershell
python experimento.py
python experimento_cascata.py
```

Para guardar evidências para o artigo, rode os scripts novamente redirecionando os resultados e salve os logs dos três serviços:

```powershell
python experimento.py | Tee-Object -FilePath resultado_retentativas.json
python experimento_cascata.py | Tee-Object -FilePath resultado_cascata.json
docker compose logs --no-color instavel normal protecao | Set-Content logs.txt
```

Esses arquivos são criados na pasta `containers`. `logs.txt` inclui os prefixos do Compose e mensagens do servidor; os **eventos de aplicação** contêm objetos JSON. Registre no artigo as configurações usadas e execute cada comparação sem tráfego externo concorrente.

Verifique os contadores diretamente, se desejar:

```powershell
Invoke-RestMethod http://127.0.0.1:8002/stats
Invoke-RestMethod http://127.0.0.1:8004/stats
```

Para repetir com outra quantidade de requisições:

```powershell
python experimento.py --requests 200
```

Para encerrar:

```powershell
docker compose down
```

Se o Docker informar que não conseguiu se conectar ao *daemon*, inicie o Docker Desktop e execute o comando novamente.

## Executar sem Docker no Windows

Esta opção executa **os dois experimentos anteriores** de retentativas e cascata. A demonstração do agente usa o Compose para persistir o SQLite e injetar o token de controle nos serviços.

Instale as dependências uma vez, na raiz do projeto:

```powershell
python -m pip install -r containers/instavel/req.txt -r containers/normal/req.txt -r containers/protecao/req.txt
```

Abra **quatro terminais PowerShell** na raiz do projeto.

Terminal 1, serviço instável:

```powershell
python -m uvicorn main:app --app-dir containers/instavel --host 127.0.0.1 --port 8002
```

Terminal 2, intermediário de proteção:

```powershell
$env:BACKEND_URL = "http://127.0.0.1:8002"
$env:NORMAL_URL = "http://127.0.0.1:8001"
python -m uvicorn main:app --app-dir containers/protecao --host 127.0.0.1 --port 8004
```

Terminal 3, serviço normal:

```powershell
$env:BACKEND_URL = "http://127.0.0.1:8002"
python -m uvicorn main:app --app-dir containers/normal --host 127.0.0.1 --port 8001
```

Terminal 4, experimentos:

```powershell
python containers/experimento.py
python containers/experimento_cascata.py
```

Abra `http://127.0.0.1:8004/dashboard` para acompanhar os eventos e as métricas. Os logs JSON também aparecem nos terminais 1 e 2.

Use `Ctrl+C` nos três primeiros terminais para encerrar os serviços.

## O que o experimento demonstra

Nos dois experimentos, a **variável alterada é a política de proteção**. A falha simulada e a quantidade de requisições são mantidas constantes dentro de cada comparação. As medidas principais são chamadas ao serviço instável, requisições barradas e sucesso ou rejeição da rota independente. Os logs com `request_id` permitem explicar os contadores por meio de exemplos concretos.

O cenário padrão envia **100 requisições lógicas por política** e força HTTP 503 em todas as chamadas durante a medição. Um resultado típico é:

| Cenário | Chamadas ao serviço instável | Cálculo |
| --- | ---: | --- |
| Sem proteção | 400 | 100 requisições × 4 tentativas |
| Apenas limite de tentativas | 200 | 100 requisições × 2 tentativas |
| Proteção completa | 10 | 5 requisições até abrir × 2 tentativas |

Nesse exemplo, `backend_call_reduction_percent` é **97,5%** em relação ao cenário sem proteção. `breaker_reduction_vs_bounded_percent` é **95%** em relação ao cenário que apenas limita as tentativas. `short_circuited` informa quantas chamadas foram barradas pelo circuito.

Cada cenário imprime um `sample_request_id`. Ao filtrar esse ID no painel, o cenário sem proteção mostra várias respostas 503 do serviço instável para a mesma requisição lógica; o cenário protegido mostra tentativas no intermediário e respostas do serviço instável com o mesmo ID.

Ao final, o script espera o período de abertura do circuito e faz uma chamada com `fail=false`. `recovery_status: 200` indica que a chamada de prova funcionou. Os contadores são comparados por diferença antes e depois de cada cenário, portanto não é necessário reiniciar os serviços para repetir o teste. Evite outras chamadas durante a execução, pois afetariam as contagens.

Os números podem variar se a execução protegida durar mais do que cinco segundos: nesse caso, o circuito poderá permitir outra chamada de prova. A medição é sequencial e todos os erros são intencionais; a redução de chamadas ao backend é o resultado principal, não a taxa de sucesso durante a falha.

### Resultado esperado da falha em cascata

`experimento_cascata.py` inicia quatro chamadas concorrentes à dependência lenta e, enquanto elas estão ativas, envia 20 chamadas à rota independente. Sem isolamento, as quatro vagas compartilhadas ficam ocupadas e a rota independente tende a ter **0 sucessos**. Com *bulkhead*, duas chamadas dependentes ocupam vagas isoladas e as outras duas são rejeitadas rapidamente; a rota independente tende a ter **20 sucessos**. Os campos `independent_success` e `independent_rejected` mostram o resultado medido.

O script também imprime IDs de exemplo. Copie um `sample_dependency_request_id` ou `sample_independent_request_id` para o filtro do painel. Nos logs da dependência, o mesmo ID aparece no serviço `normal` e no serviço `instavel`, mostrando o caminho da chamada. A hora UTC em `timestamp` permite ordenar os eventos; `scenario`, `event`, `status` e `duration_ms` ajudam a explicar o efeito da falha.

Exemplo de um evento JSON, com ID ilustrativo:

```json
{"timestamp":"2026-10-27T12:00:00+00:00","service":"normal","event":"dependency_finished","request_id":"exemplo-123","scenario":"cascade_unsafe","status":503,"duration_ms":1500}
```

## Endpoints úteis

| Serviço | Endpoint | Finalidade |
| --- | --- | --- |
| Instável (`localhost:8002`) | `GET /work?fail=true` | Simula HTTP 503. |
| Instável (`localhost:8002`) | `GET /work?fail=false` | Simula HTTP 200. |
| Instável (`localhost:8002`) | `GET /stats` | Mostra total de chamadas, falhas e sucessos. |
| Normal (`localhost:8001`) | `GET /dependent?mode=unsafe` | Chama a dependência lenta usando capacidade compartilhada. |
| Normal (`localhost:8001`) | `GET /dependent?mode=protected` | Chama a dependência lenta usando capacidade isolada. |
| Normal (`localhost:8001`) | `GET /independent?mode=unsafe` | Testa o efeito da capacidade compartilhada. |
| Normal (`localhost:8001`) | `GET /independent?mode=protected` | Testa se a rota independente continua disponível. |
| Normal (`localhost:8001`) | `GET /stats` | Mostra sucessos, rejeições e chamadas dependentes ativas. |
| Proteção (`localhost:8004`) | `GET /work?fail=true` | Usa a política de proteção para chamar o serviço instável. |
| Proteção (`localhost:8004`) | `GET /stats` | Mostra tentativas, chamadas barradas e estado do circuito. |
| Proteção (`localhost:8004`) | `GET /observability` | Reúne logs recentes e métricas dos dois serviços. |
| Proteção (`localhost:8004`) | `GET /dashboard` | Mostra o painel ao vivo. |

A rota `/health` do serviço instável retorna falha após 30 segundos enquanto a anomalia automática está ativa; depois que o agente a desativa, volta a indicar `healthy`. A rota `/health` de `carga` também passa a indicar `healthy` após o reparo. Os experimentos de retentativas e cascata medem `/work` e não dependem desses health checks.

## Limites

Este é um protótipo com uma instância por serviço. Estoque, pedidos, pagamentos, eventos recentes e contadores ficam em memória e reiniciam com os serviços; o agente persiste sua cópia dos eventos e suas decisões em SQLite. O primeiro experimento mede **amplificação de chamadas durante indisponibilidade total**. O segundo demonstra **propagação da lentidão por capacidade compartilhada**. Ainda não há métricas de produção, rastreamento distribuído completo ou múltiplos níveis de dependência. O parâmetro `fail` existe somente para controlar a simulação.
