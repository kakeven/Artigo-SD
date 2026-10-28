# Algoritmos modelo e memória do agente

Este documento explica a implementação técnica do detector Random Forest, da consulta ao modelo Qwen e da memória de incidentes. Os três métodos exibidos no relatório compartilham **o mesmo detector Random Forest**. Eles diferem na forma de escolher a ação depois que a falha é detectada. O [documento funcional](funcionamento_das_estrategias.md) descreve o fluxo da loja; o [relatório de 15 episódios](exemplos/comparacao_loja.html) mostra os resultados observados.

## Separação das responsabilidades

O agente resolve duas perguntas em sequência:

1. **Há um padrão de falha?** A Random Forest recebe apenas uma sequência recente de códigos HTTP e classifica o padrão como `normal`, `intermittent` ou `continuous`.
2. **Qual ação deve ser tentada?** A estratégia `rf` usa uma regra fixa, `memory` consulta casos verificados antes da regra fixa, e `qwen_memory` consulta casos verificados antes de chamar Qwen para um erro desconhecido.

O nome do erro observado nos logs não participa das quatro características usadas pela Random Forest. Ele entra **depois da classificação**, na busca da memória ou no prompt enviado ao Qwen. Por isso, duas causas que produzem uma sequência parecida de HTTP 503 podem receber a mesma classificação e ainda exigir reparos diferentes.

```text
Eventos HTTP ──► características numéricas ──► Random Forest ──► padrão + confiança
                                                               │
                               serviço + nome do erro ────────┤
                                                               ▼
                                         regra | memória | Qwen
                                                               ▼
                                             ação permitida ──► /repair
                                                               ▼
                                            chamada de prova e registro
```

## Como a Random Forest é treinada

O arquivo `containers/agente/model.py` cria o classificador quando o processo do agente inicia. O treinamento é **sintético e local**: um gerador com semente `42` constrói 300 grupos de exemplos. Cada grupo contém uma sequência saudável, uma intermitente e uma contínua, totalizando **900 exemplos**. O comprimento de cada sequência é sorteado entre 6 e 20 respostas.

- **Normal:** respostas 200; algumas sequências mais longas recebem um erro 503 isolado.
- **Intermitente:** mistura de 200 e 503, com alternâncias produzidas pelo gerador.
- **Contínua:** respostas 503; algumas sequências mais longas recebem um sucesso 200 isolado.

Cada código HTTP é transformado em `erro = 1` quando `status >= 500` e `erro = 0` nos demais casos. Para uma sequência com `n` respostas, a função de características calcula:

| Característica | Cálculo | O que representa |
| --- | --- | --- |
| Fração de erros | quantidade de respostas 5xx ÷ `n` | Intensidade da falha |
| Maior série de erros | maior número de erros consecutivos ÷ `n` | Persistência da falha |
| Alternâncias | mudanças entre erro e não erro ÷ `max(1, n − 1)` | Intermitência |
| Última resposta | `1` se a última resposta é 5xx; `0` caso contrário | Estado mais recente |

Exemplo: `[200, 503, 200, 503, 200, 503]` vira `[0, 1, 0, 1, 0, 1]` e produz o vetor `[0,5; 1/6; 1; 1]`. Uma sequência `[503, 503, 503, 503, 503, 503]` produz `[1; 1; 0; 1]`. A árvore não recebe diretamente o nome `OverloadError`, o código-fonte nem o ID do pedido.

O classificador é `RandomForestClassifier(n_estimators=80, random_state=42, min_samples_leaf=2)`. Em termos práticos, ele treina 80 árvores de decisão sobre amostras dos exemplos. Na previsão, `predict_proba` devolve probabilidades por classe; o agente escolhe a classe com maior probabilidade e usa esse valor como confiança. **Não há novo treinamento da Random Forest durante os episódios.** O modelo é reconstruído com a mesma semente quando o processo reinicia.

## Quando a classificação autoriza uma tentativa de reparo

O agente consulta `/events` dos serviços aproximadamente a cada segundo, guarda os eventos no SQLite e considera até **12 status HTTP recentes** do episódio. Antes de tentar qualquer ação, exige:

1. pelo menos seis novas respostas do serviço e pelo menos seis status na janela consultada;
2. falha simulada ainda ativa no serviço afetado;
3. classe prevista diferente de `normal`;
4. confiança de pelo menos `0,7`;
5. pelo menos três respostas 5xx na janela;
6. intervalo de pelo menos 30 segundos desde a última tentativa de ação naquele serviço, quando houver repetição.

O serviço `normal` é usado como controle saudável e não recebe ação de reparo. O agente atua nos serviços `instavel` (estoque) ou `carga` (pagamento). Após a detecção, ele consulta os nomes de erro recentes do serviço, ocorridos desde o início do episódio, e usa o nome mais frequente como pista para a decisão.

## Estratégia 1 Random Forest com regra fixa

O identificador técnico desta estratégia é `rf`. Ela **não usa a saída da floresta para prever diretamente uma ação**. A floresta decide apenas se o padrão é anormal o suficiente para agir. A ação é escolhida por uma regra fixa baseada no serviço:

```text
se serviço == instavel: ação = disable_simulated_fault
senão, se serviço == carga: ação = reset_overload
```

Assim, o agente consegue corrigir a sobrecarga do pagamento, cuja ação esperada é `reset_overload`. Uma falha diferente no mesmo serviço, como `DependencyTimeoutError`, continua produzindo HTTP 503, mas exige `restore_dependency`. Nesse caso a regra fixa escolhe uma ação permitida, porém incompatível com a causa ativa; o serviço rejeita o reparo.

Esta estratégia é a **referência sem memória**. Ela permite medir quanto se obtém apenas com classificação do padrão e uma política fixa de reparo.

## Como a memória é representada

O arquivo `containers/agente/repository.py` mantém a tabela SQLite `incident_memory`. Cada registro contém:

```text
target          serviço afetado, como carga ou instavel
error_name      nome do erro observado, como OverloadError
action          ação que foi verificada para esse par
verified_count  número de reparos verificados com essa ação
```

A chave é `(target, error_name, action)`. A consulta só considera uma ação reutilizável quando `verified_count >= 3`. A busca exige correspondência de **serviço e nome do erro**; um sucesso no pagamento não é automaticamente transferido para o estoque. Se mais de uma ação alcançar o limite para o mesmo par, a consulta prefere a maior contagem. A ação encontrada ainda precisa pertencer à lista de ações permitidas para o serviço.

Depois de tentar um reparo, o agente exige uma prova: HTTP 200 no roteador, resposta do próprio serviço afetado e `/fault` indicando `enabled = false`. Apenas então grava ou incrementa o caso na memória. A memória não guarda os parâmetros das árvores e não modifica os pesos de nenhum modelo. Trata-se de uma **consulta a casos de reparo verificados**, não de aprendizagem por reforço ou ajuste incremental do Qwen.

## Estratégia 2 Random Forest com memória

O identificador técnico é `memory`. A classificação e os limites para agir são iguais aos da estratégia `rf`. O que muda é a função que escolhe a ação:

```text
erro = nome mais frequente entre até 12 erros recentes do serviço neste episódio
ação = memória.buscar(serviço, erro, mínimo_de_verificações=3)
se ação não existir ou não for permitida:
    ação = regra_fixa(serviço)
```

Quando a memória contém um caso correspondente, a decisão registrada tem origem `memory`. Quando não contém, a origem é `rf_fallback`. Um reparo verificado aumenta `verified_count` para o par serviço, erro e ação. Uma ação rejeitada ou uma chamada de prova que falhe não aumenta a contagem.

Na comparação atual, a memória inicial contém `carga + OverloadError + reset_overload`, com contagem `3`. Por isso a sobrecarga é tratada pela memória. Para as outras quatro causas, essa memória inicial não oferece um caso correspondente e a estratégia aplica a regra fixa. **A memória não generaliza sozinha para nomes de erro que ainda não conhece.**

## O modelo Qwen e sua consulta

O identificador `qwen_memory` não significa um detector novo. O detector continua sendo a Random Forest. A estratégia primeiro faz **a mesma busca na memória**. Se encontrar um caso com três verificações, usa a ação lembrada e não chama Qwen.

Se não houver caso suficiente, `containers/agente/strategies.py` monta um prompt com o serviço afetado, o nome do erro observado, a sequência HTTP recente, as ações permitidas e até 16.000 caracteres do código-fonte do serviço. O código é montado no contêiner do agente somente para leitura. A chamada vai à API local `/api/generate` do Ollama com o modelo configurado `qwen2.5:1.5b-instruct-q4_K_M`, saída em formato JSON, `temperature = 0`, `num_ctx = 4096` e tempo máximo de 45 segundos.

O formato solicitado é `{"action": "nome_da_ação"}`. O agente extrai esse campo e aceita a proposta apenas se ela estiver no conjunto permitido para o serviço:

| Serviço | Ações permitidas |
| --- | --- |
| Estoque | `disable_simulated_fault`, `restore_inventory_dependency`, `release_reservation_lock` |
| Pagamento | `reset_overload`, `restore_dependency`, `reset_circuit_breaker` |

Se Ollama não responder, o JSON for inválido ou a ação estiver fora da lista, a função retorna `None`. A decisão é registrada como `no_action`, com origem `qwen_unavailable_or_invalid`; **não há retorno automático à regra fixa** nesse caso. Se Qwen propuser uma ação da lista, o agente chama `/repair` e faz a mesma verificação usada pelas outras estratégias. Uma ação permitida ainda pode ser errada para a causa atual: o serviço rejeita a incompatibilidade e a decisão fica sem verificação positiva.

Quando a ação proposta por Qwen é verificada, ela entra na mesma memória SQLite. Depois de três sucessos verificados para o mesmo serviço e nome de erro, episódios futuros podem reutilizá-la sem chamar Qwen. Os pesos do Qwen permanecem iguais; a mudança ocorre na tabela de casos.

## Interação entre detector modelo e memória

| Etapa | `rf` | `memory` | `qwen_memory` |
| --- | --- | --- | --- |
| Detectar padrão de falha | Random Forest | Random Forest | Random Forest |
| Procurar ação verificada na memória | Não | Sim | Sim |
| Se a memória não conhecer o erro | Regra fixa | Regra fixa | Consulta Qwen |
| Se Qwen estiver indisponível ou inválido | Não se aplica | Não se aplica | `no_action` |
| Verificar o serviço após uma ação | Sim | Sim | Sim |
| Registrar reparo verificado na memória | Sim, embora não consulte a memória | Sim | Sim |

O ponto central é que **detecção, escolha de ação e aprendizagem de casos são mecanismos separados**. A Random Forest não recebe a memória como característica. A memória não altera o classificador. O Qwen não substitui a floresta e só é consultado quando a memória não encontra uma ação suficientemente verificada.

## Preparação da memória e interpretação dos resultados

O script `containers/comparar_loja.py` começa limpando a memória e executa três episódios de sobrecarga com a estratégia `rf`. Cada reparo precisa ser verificado. Em seguida, salva a memória obtida como referência e a restaura antes de cada um dos **15 episódios medidos**. O estoque também é reiniciado antes de cada episódio. Assim, um caso descoberto no primeiro teste medido não é herdado pelo seguinte.

Na execução registrada, `rf` recuperou 1 de 5 causas, `memory` recuperou 1 de 5 e `qwen_memory` recuperou 4 de 5. Na causa `InventoryDependencyError`, Qwen propôs `disable_simulated_fault` em vez de `restore_inventory_dependency`; a ação foi rejeitada e não houve recuperação confirmada. O [relatório](exemplos/comparacao_loja.html) permite conferir cada episódio, e o [JSON](exemplos/comparacao_loja.json) contém os campos técnicos de cada decisão.

Esses números representam **uma execução de cada causa por estratégia**. Como o prompt inclui o nome do erro e o código-fonte com as ações possíveis, um acerto mostra o uso dessas pistas explícitas no laboratório; não demonstra descoberta autônoma de bugs desconhecidos. Repetições por causa e casos fora desse conjunto seriam necessários para estimar variabilidade e generalização.

## Arquivos de referência

- `containers/agente/model.py`: geração das 900 amostras sintéticas, características e Random Forest.
- `containers/agente/service.py`: coleta dos eventos, limites para agir, execução e verificação do reparo.
- `containers/agente/strategies.py`: regras fixas, ordem memória → Qwen e validação das ações.
- `containers/agente/repository.py`: esquema e consultas da memória SQLite.
- `containers/comparar_loja.py`: preparo da memória de referência e execução dos 15 episódios.
