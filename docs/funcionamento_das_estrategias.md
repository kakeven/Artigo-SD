# Funcionamento das três estratégias de reparo

Este documento explica como o agente atua na loja de roupas simulada e o que muda entre **Random Forest**, **Random Forest com memória** e **Qwen com memória**. O foco é a decisão tomada após a detecção de uma falha. O [relatório visual](exemplos/comparacao_loja.html) mostra os pedidos e os resultados dos 15 episódios executados; os [dados completos](exemplos/comparacao_loja.json) preservam as decisões e os identificadores de correlação.

## O que as três versões têm em comum

Um pedido passa pelo serviço de pedidos (`pedidos`), pela reserva de estoque (`estoque`) e pelo pagamento fictício (`pagamentos`). A comparação ativa uma causa técnica em **um** dos dois últimos serviços por episódio. Os serviços registram eventos JSON com horário, serviço, status HTTP, nome do erro e ID de correlação. O agente consulta `/events` aproximadamente a cada segundo e guarda os eventos em SQLite.

Nas três estratégias, a detecção é feita pela **mesma Random Forest**. Ela foi treinada ao iniciar o agente com sequências sintéticas de respostas normais, intermitentes e continuamente falhas. Para cada sequência recente, calcula quatro características: proporção de respostas 5xx, maior série consecutiva de erros, frequência de alternância entre sucesso e erro e resultado da última resposta. O classificador indica `normal`, `intermittent` ou `continuous`, com uma confiança.

O agente só tenta um reparo quando há pelo menos seis respostas recentes do serviço, três ou mais respostas 5xx, classificação diferente de `normal` e confiança de pelo menos 0,7. Ele também verifica se a falha simulada ainda está ativa. Portanto, **Qwen não substitui o detector**: sua participação, quando ocorre, é escolher a ação de reparo.

Depois da escolha, o agente chama `/repair` no serviço afetado com um token de controle. Uma ação incompatível com a causa ativa é recusada. Para marcar o reparo como verificado, o agente faz uma nova chamada pelo roteador e exige HTTP 200, resposta do próprio serviço afetado e estado de falha desativado. Só um reparo verificado entra na memória de casos. A decisão, a origem da ação e a verificação ficam registradas no SQLite.

```text
Pedido → evento do serviço → coleta no SQLite → Random Forest detecta o padrão
                                               ↓
                                estratégia escolhe o reparo
                                               ↓
                         serviço executa ou rejeita a ação
                                               ↓
                          agente testa o próprio serviço
                                               ↓
                         decisão registrada; memória atualizada se verificada
```

## 1. Random Forest sem memória (`rf`)

**Entrada da decisão:** serviço afetado e padrão detectado. A Random Forest identifica que há uma falha, mas não determina qual causa específica gerou os HTTP 503. A escolha do reparo é uma regra fixa por serviço:

| Serviço | Ação escolhida |
| --- | --- |
| Estoque (`estoque`) | `disable_simulated_fault` |
| Pagamento (`pagamentos`) | `reset_overload` |

**Passo a passo:** o agente coleta respostas recentes, classifica o padrão com a Random Forest, seleciona a ação padrão do serviço, chama `/repair` e verifica a resposta. Ele não consulta casos anteriores nem Qwen.

**Consequência:** a estratégia acerta quando a causa ativa corresponde à ação padrão. Na sobrecarga do pagamento, por exemplo, `reset_overload` remove a falha. Se o pagamento estiver com a dependência indisponível, a mesma ação é rejeitada: o status HTTP 503 e o serviço afetado não bastam para distinguir as duas causas.

O nome `rf` identifica a estratégia de referência. Tecnicamente, **a Random Forest classifica o padrão**, enquanto a escolha da ação é a regra fixa descrita acima.

## 2. Random Forest com memória (`memory`)

**Entrada da decisão:** a mesma detecção da Random Forest, o serviço afetado e o nome do erro observado nos eventos. A memória SQLite guarda combinações de **serviço + nome do erro + ação** e quantas vezes aquela ação resolveu o problema com verificação positiva.

**Passo a passo:** após a detecção, o agente procura uma ação já verificada pelo menos **três vezes** para o mesmo serviço e nome de erro. Se encontrar uma ação permitida, reutiliza essa ação. Se não encontrar, aplica a mesma regra fixa da estratégia `rf`. Em seguida chama `/repair`, verifica o serviço e registra o resultado. Um novo sucesso aumenta a contagem do caso na memória; uma tentativa sem verificação positiva não aumenta.

**Exemplo:** a memória preparada para a comparação contém `pagamentos + OverloadError → reset_overload`, com três reparos verificados. Quando a sobrecarga reaparece, a ação vem da memória. Para `DependencyTimeoutError`, não havia caso preparado: a estratégia recorreu à regra fixa `reset_overload`, que não corrigiu essa causa.

Essa memória **não altera os pesos** da Random Forest. Ela é uma tabela de experiências verificadas. O benefício depende da existência de um caso correspondente; para um erro novo, o comportamento volta a ser o da regra padrão.

## 3. Qwen com memória (`qwen_memory`)

**Entrada da decisão:** o mesmo detector Random Forest, o mesmo banco de memória, o nome do erro, a sequência recente de respostas HTTP e o código-fonte do serviço afetado montado no contêiner do agente somente para leitura. O modelo local configurado é `qwen2.5:1.5b-instruct-q4_K_M`, acessado pelo Ollama.

**Passo a passo:** primeiro, o agente procura na memória um caso com pelo menos três reparos verificados, exatamente como em `memory`. **Se encontrar, usa a memória e não chama Qwen.** Se o erro ainda não tiver um caso suficiente, envia ao Qwen as pistas observadas, o trecho de código e a lista de ações permitidas para aquele serviço. Pede uma resposta JSON com uma única ação. O agente aceita apenas uma ação dessa lista; saída inválida ou indisponibilidade do modelo gera `no_action`. Uma resposta válida ainda precisa ser aceita pela API `/repair` e passar pela verificação do próprio serviço.

**Exemplos da execução registrada:** para `OverloadError`, esta estratégia usou a memória preparada, sem consulta ao Qwen. Para o tempo esgotado no pagamento, Qwen escolheu `restore_dependency` e o reparo foi verificado. Para a dependência do estoque indisponível, Qwen escolheu `disable_simulated_fault`, ação incompatível com a causa; o reparo não foi confirmado. Isso mostra que a lista de ações permitidas impede uma ação fora do conjunto, mas **não garante que a ação permitida escolhida seja correta**.

O Qwen não executa código, não recebe a verdade escondida do experimento e não atualiza seus próprios pesos. Ele propõe uma ação; o agente valida a resposta e o serviço decide se ela corresponde à falha simulada. Os acertos desta prova de conceito também dependem de pistas explícitas no nome do erro e no código-fonte apresentado ao modelo.

## As cinco causas e as ações esperadas

Todas as causas abaixo produzem falhas HTTP 503 durante a simulação. O código do serviço só desativa a causa quando recebe a ação correspondente.

| Causa | Serviço | Nome de erro observado | Ação esperada |
| --- | --- | --- | --- |
| Sobrecarga no pagamento | Pagamento | `OverloadError` | `reset_overload` |
| Tempo esgotado na dependência do pagamento | Pagamento | `DependencyTimeoutError` | `restore_dependency` |
| Circuito do pagamento preso em aberto | Pagamento | `CircuitOpenError` | `reset_circuit_breaker` |
| Dependência do estoque indisponível | Estoque | `InventoryDependencyError` | `restore_inventory_dependency` |
| Reserva de estoque bloqueada | Estoque | `ReservationLockError` | `release_reservation_lock` |

As duas falhas de estoque novas simulam indisponibilidade ou bloqueio da reserva; não representam uma falta real de peças. O pagamento é fictício e não movimenta dinheiro.

## Como a comparação é executada

O comando `python containers/executar_loja.py` prepara os contêineres e chama a comparação. Antes dos episódios medidos, o script limpa a memória e executa **três reparos verificados de sobrecarga** para formar um caso conhecido. Ele salva essa memória como referência. Antes de **cada** episódio medido, restaura a mesma memória e reinicia o estoque. Assim, um episódio não ensina o seguinte.

Cada estratégia recebe as **mesmas cinco causas**, uma por episódio. Em cada episódio, a sequência de peças, tamanhos e quantidades é a mesma: são enviados 22 pedidos iniciais, o script aguarda a decisão do agente e envia mais três pedidos para verificar a recuperação. Os IDs dos pedidos são diferentes, e o tempo de resposta pode variar. A memória inicial contém apenas o caso conhecido de sobrecarga. São **15 episódios medidos** ao todo: cinco por estratégia. Os três episódios usados para preparar a memória ficam fora dessa contagem.

O relatório considera uma causa recuperada quando há uma decisão de reparo verificada para o serviço afetado **e** os três pedidos finais são confirmados. Ele mostra também quantos pedidos foram confirmados no total e o tempo entre o início do episódio e a decisão verificada. A sequência colorida conserva a ordem dos pedidos e marca a primeira ação do agente. O JSON preserva os dados individuais para auditoria.

## Resultado da execução de 28 de outubro de 2026

| Causa | Random Forest | RF com memória | Qwen com memória |
| --- | --- | --- | --- |
| Sobrecarga no pagamento | Reparado | Reparado pela memória | Reparado pela memória |
| Tempo esgotado no pagamento | Não reparado | Não reparado | Reparado pelo Qwen |
| Circuito do pagamento preso em aberto | Não reparado | Não reparado | Reparado pelo Qwen |
| Dependência do estoque indisponível | Não reparado | Não reparado | Não reparado; Qwen escolheu outra ação |
| Reserva de estoque bloqueada | Não reparado | Não reparado | Reparado pelo Qwen |
| **Total de causas recuperadas** | **1/5** | **1/5** | **4/5** |

Nesta execução, Qwen com memória recuperou mais causas. O resultado mostra o comportamento **destes cinco cenários com uma execução por causa**. Ele não mede a variabilidade entre repetições da mesma causa e não prova superioridade estatística ou desempenho em produção. A memória foi preparada apenas com sobrecarga; isso favorece deliberadamente o estudo de como as estratégias lidam com erros ainda não conhecidos.

## Onde conferir no projeto

- `containers/agente/model.py`: características e treinamento da Random Forest.
- `containers/agente/service.py`: coleta, condições para agir, chamada de reparo e verificação.
- `containers/agente/strategies.py`: regra padrão, consulta à memória, prompt e validação da resposta do Qwen.
- `containers/agente/repository.py`: eventos, decisões e memória no SQLite.
- `containers/demo_loja.py`: configuração de uma causa e sequência de pedidos.
- `containers/comparar_loja.py`: preparo da memória e execução dos 15 episódios.
- `containers/relatorio_loja.py`: geração do HTML comparativo.
