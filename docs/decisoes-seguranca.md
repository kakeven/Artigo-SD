# Decisões de segurança do laboratório

- **Fronteira:** os serviços e o agente rodam em uma rede Docker local. Portas publicadas no host devem usar `127.0.0.1`. O painel é apenas de leitura; mudar o roteamento ou o estado das falhas simuladas é uma operação administrativa.
- **Dados:** os logs contêm IDs aleatórios, serviço, evento, horário, status e duração. O laboratório não precisa de nomes, senhas ou dados pessoais. O token de controle não entra em logs nem respostas.
- **Autorização:** o agente usa um token Bearer compartilhado para desligar a falha simulada em `instavel` ou `carga`. A API rejeita token ausente ou incorreto. O roteador também conserva um controle de desvio para testes manuais; ambas as ações são limitadas a alvos conhecidos.
- **Segredo:** o token é gerado localmente em `containers/.env`, ignorado pelo Git, e injetado nos contêineres. Não há token padrão no código ou no Compose.
- **Persistência:** o agente grava logs e decisões em SQLite com consultas parametrizadas, em volume Docker. O banco não publica porta. O arquivo pode ser copiado para backup experimental; `docker compose down -v` apaga o volume.
- **Exposição:** o protótipo é para uso local. Antes de qualquer acesso remoto, adicionar HTTPS, autenticação para o painel, regras de firewall/proxy e revisão de dependências.
- **Verificação:** testar recusa da API administrativa sem token e com token inválido, validação de alvo/ação, ausência de segredo nos logs e recuperação após a ação. Usar OWASP ASVS e OWASP Top 10 como checklist adaptado ao protótipo.
