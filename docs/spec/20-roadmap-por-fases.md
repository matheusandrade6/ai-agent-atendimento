## 20. Roadmap por fases

### Fase 0 — Fundação (semana 1)

**Entregáveis:** repo, Docker Compose (Postgres+Redis), migrations com o schema da seção 9, RLS, config loader validado, esqueleto FastAPI, telemetria, CI.
**Aceite:** `docker compose up` sobe tudo; `pytest` verde; um tenant de exemplo carregado do YAML; teste de isolamento entre dois tenants passando.

### Fase 1 — Agente conversacional (semanas 2–3)

**Entregáveis:** webhook WhatsApp com assinatura e dedup; debounce; worker inbound/outbound; motor do agente com tool calling; tools de leitura (`search_knowledge`, `list_services`); RAG com pgvector; guardrails de entrada e saída; `escalate_to_human`; suíte conversacional inicial.
**Aceite:** o agente responde dúvidas do tenant de exemplo pelo WhatsApp com p95 < 5s; declara desconhecimento corretamente; escala por keyword e por pedido; nenhum cenário conversacional de escopo falha.

### Fase 2 — Agendamento (semanas 4–6)

**Entregáveis:** `CalendarProvider` + Google (OAuth, freebusy, insert/patch/delete); motor de disponibilidade completo; hold + advisory lock + constraint de exclusão; tools de escrita; fluxos de agendar/remarcar/cancelar; mensagem de confirmação; `slot_token` assinado.
**Aceite:** agendamento ponta a ponta cria evento correto na agenda Google; teste de concorrência com 20 requisições simultâneas produz exatamente 1 agendamento; horário de verão e feriado tratados; nenhuma resposta com horário fora do resultado de `check_availability`.

### Fase 3 — Operação (semanas 7–8)

**Entregáveis:** painel (conversas, takeover, agenda, serviços, horários, base de conhecimento); lembretes com templates utility; sync reverso do Google (watch + syncToken); notificações de handoff; métricas da seção 17; widget web.
**Aceite:** atendente assume e devolve conversa sem perda de contexto; lembrete de 24h sai dentro do horário comercial e usa template quando a janela está fechada; cancelamento feito direto no Google notifica o contato em até 15 min; widget funciona em domínio de teste.

### Fase 4 — Escala e produtização (semanas 9–12)

**Entregáveis:** script de onboarding; lista de espera e encaixe; painel de custo por tenant; retenção/expurgo e fluxo de exclusão LGPD; documentação de operação; `internal.py` (agenda própria) como segundo `CalendarProvider`.
**Aceite:** onboarding completo de um tenant novo em ≤ 4h medido em cronômetro; encaixe funcionando; 3 tenants em produção com métricas separadas.

---
