## 18. Segurança e LGPD

### 18.1 Segurança

- Segredos em secret manager; nunca no YAML (YAML usa `${VAR}`).
- Validação de assinatura em todo webhook.
- RLS no Postgres + `app.tenant_id` por sessão; testes automatizados de vazamento entre tenants.
- Painel com sessão curta, CSRF, rate limit no login.
- Conteúdo recebido do usuário nunca é interpolado como instrução no prompt — vai sempre em bloco de conteúdo delimitado.
- Dependências com scan automático; imagens Docker sem root.

### 18.2 LGPD — bases e minimização

- **Base legal:** execução de contrato / procedimento preliminar (art. 7º, V) para dados de agendamento. Para dados de saúde (art. 11), **consentimento específico e destacado** ou tutela da saúde por profissional — definir por tenant e registrar em `contacts.consent`.
- **Minimização:** o agente coleta apenas os `intake_fields` configurados. Nada de CPF, endereço completo ou dado clínico sem necessidade explícita.
- **Proibição explícita no prompt e no guardrail:** não solicitar CPF, RG, cartão, dados bancários ou histórico clínico detalhado. Se o usuário enviar espontaneamente, o dado é redigido no log e sinalizado.

### 18.3 Retenção e direitos

- Retenção default: mensagens 24 meses, agendamentos 5 anos (interesse legítimo/obrigação do prestador), mídia 6 meses. Configurável por tenant.
- Job de expurgo diário.
- Endpoint e procedimento para exclusão a pedido do titular, com anonimização preservando o histórico de agendamento agregado.
- Registro de tratamento e DPA entre Datamind (operadora) e cliente (controlador) — **artefato comercial obrigatório antes do go-live**.

### 18.4 Transparência

`persona.introduce_as_ai: true` por padrão. O agente se identifica como assistente virtual na primeira mensagem de cada conversa nova e sempre que perguntado diretamente. Não simula ser humano em nenhuma hipótese.

---
