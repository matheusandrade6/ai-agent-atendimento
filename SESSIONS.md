# Plano de Sessões — Datamind Agenda AI

Este documento fatia a spec (`docs/SPEC.md`, seções isoladas em `docs/spec/`) em **24 sessões
de trabalho independentes**. Cada sessão foi desenhada para caber num contexto pequeno:
ela declara exatamente **o que ler**, **o que entregar** e **como saber que terminou**.

## Como usar

1. Abra uma **sessão nova do Claude Code** por item (não reaproveite contexto entre sessões).
2. Escolha o modelo pela coluna **Modelo** da tabela.
3. Cole o **prompt de abertura** da sessão (blocos no final deste arquivo).
4. Ao terminar, atualize a coluna **Status** aqui e faça commit.

**Nunca leia `docs/SPEC.md` inteiro dentro de uma sessão** (71 KB ≈ 20k tokens). Leia só os
arquivos de `docs/spec/` que a sessão listar. `CLAUDE.md` já carrega as regras de ouro
automaticamente em toda sessão.

## Critério de escolha de modelo

| Modelo | Quando |
|---|---|
| **Haiku 4.5** | Trabalho mecânico: boilerplate, CRUD, templates, scripts. Sem decisão de design. |
| **Sonnet 5** | Implementação padrão com regra de negócio conhecida, testes, integração HTTP. |
| **Opus 5** | Concorrência, aritmética de tempo/timezone, segurança, engenharia de prompt, algoritmo com muitos casos de borda, decisão arquitetural. |

Regra prática: se um erro sutil passar despercebido e virar **overbooking, vazamento entre
tenants, alucinação de horário/preço ou perda de mensagem**, use Opus.

---

## Mapa das sessões

| # | Sessão | Fase | Modelo | Depende de | Status |
|---|---|---|---|---|---|
| S01 | Scaffolding, Docker, settings, CI | 0 | Sonnet | — | ✅ feito |
| S02 | Modelo de dados, migrations e RLS | 0 | **Opus** | S01 | ✅ feito |
| S03 | Config de tenant (schema Pydantic + loader YAML) | 0 | Sonnet | S01 | ✅ feito |
| S04 | Webhook WhatsApp: assinatura, dedup, roteamento | 1 | Sonnet | S02, S03 | ✅ feito |
| S05 | Filas ARQ, debounce Redis, workers in/out | 1 | **Opus** | S04 | ✅ feito |
| S06 | LLMProvider + engine de tool calling + prompt builder | 1 | **Opus** | S03, S05 | ✅ feito |
| S07 | Base de conhecimento: ingestão e recuperação (pgvector) | 1 | Sonnet | S02 | ✅ feito |
| S08 | Tools de leitura: `search_knowledge`, `list_services` | 1 | Sonnet | S06, S07 | ✅ feito |
| S09 | Guardrails de entrada e saída | 1 | **Opus** | S06 | ⬜ |
| S10 | Handoff e `escalate_to_human` | 1 | Sonnet | S06, S09 | ⬜ |
| S11 | Runner da suíte conversacional | 1 | **Opus** | S06, S08, S09 | ⬜ |
| S12 | `CalendarProvider` + Google (OAuth, freebusy, events) | 2 | **Opus** | S02 | ⬜ |
| S13 | Motor de disponibilidade | 2 | **Opus** | S12, S03 | ⬜ |
| S14 | Hold, advisory lock, confirmação idempotente | 2 | **Opus** | S13 | ⬜ |
| S15 | `slot_token` assinado + tools de escrita | 2 | Sonnet | S14 | ⬜ |
| S16 | Fluxos de remarcação e cancelamento | 2 | Sonnet | S15 | ⬜ |
| S17 | Painel: auth, caixa de conversas, takeover | 3 | Sonnet | S10 | ⬜ |
| S18 | Painel: agenda, serviços, horários, base, config | 3 | Sonnet | S17 | ⬜ |
| S19 | Lembretes, cron e templates utility | 3 | **Opus** | S14, S05 | ⬜ |
| S20 | Sync reverso do Google Calendar | 3 | **Opus** | S12, S14 | ⬜ |
| S21 | Widget web (canal `web`) | 3 | Sonnet | S06 | ⬜ |
| S22 | Métricas de produto e custo por tenant | 3 | Sonnet | S06, S17 | ⬜ |
| S23 | Script de onboarding de tenant | 4 | Haiku/Sonnet | S03 | ⬜ |
| S24 | Lista de espera, LGPD e agenda própria | 4 | Sonnet | S14, S19 | ⬜ |

**Caminho crítico:** S01 → S02 → S05 → S06 → S12 → S13 → S14. As demais paralelizam.

---

## Prompts de abertura

Copie o bloco inteiro na primeira mensagem da sessão nova.

### S04 — Webhook WhatsApp · Sonnet

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/14-integracoes.md (14.1), docs/spec/09-modelo-de-dados.md
      (tabelas conversations, messages, contacts).
Entregue a Sessao S04 de SESSIONS.md: GET/POST /webhooks/whatsapp com validacao
HMAC X-Hub-Signature-256 ANTES do parsing, dedup por provider_msg_id, roteamento
por phone_number_id -> tenant, persistencia de contact/conversation/message,
atualizacao de service_window_expires_at, resposta 200 em <1s e enfileiramento.
Aceite: testes de assinatura invalida (403), payload duplicado (1 linha),
payload malformado (200 sem crash), status callbacks atualizando messages.status.
```

### S05 — Filas, debounce, workers · Opus

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/07-arquitetura.md, docs/spec/14-integracoes.md (14.1.2, 14.1.3),
      docs/spec/11-motor-do-agente.md (11.1).
Entregue a Sessao S05 de SESSIONS.md: setup ARQ, worker inbound com agregacao por
debounce em Redis (chave agg:{conversation_id}, cada mensagem reinicia o timer),
worker outbound com backoff exponencial e tratamento de 429, cron worker vazio
(hooks para S19/S20), rate limit por contato.
Cuidados: a agregacao nao pode disparar dois turnos para a mesma rajada nem
perder a ultima mensagem que chega junto do disparo. Escreva o teste dessa corrida.
Aceite: teste de 3 mensagens em 2s produz exatamente 1 turno com o texto concatenado
na ordem; reentrega do webhook nao duplica turno.
```

### S06 — Engine do agente · Opus

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/11-motor-do-agente.md (inteira),
      docs/spec/10-configuracao-por-tenant.md, docs/spec/12-fluxos-conversacionais.md.
Consulte a skill `claude-api` antes de escrever a chamada ao modelo.
Entregue a Sessao S06 de SESSIONS.md: interface LLMProvider + implementacao Anthropic,
app/agent/prompt.py com os blocos nomeados da 11.2 (um builder por bloco, testavel
isoladamente), app/agent/engine.py com o loop de tool calling (MAX_TOOL_ITERATIONS=6,
tenant_id sempre do contexto e nunca dos args, audit_log por chamada),
app/agent/memory.py (janela de 20 mensagens + resumo rolante a cada 15, calculo de
collected/missing EM CODIGO), contabilizacao de tokens/custo por mensagem.
Cuidados: conteudo do usuario entra sempre em bloco delimitado, nunca interpolado
como instrucao. Nenhuma tool de escrita e chamada duas vezes com a mesma chave de
idempotencia no mesmo turno.
Aceite: testes por bloco de prompt; teste do loop com tool falsa; teste de que
`missing` vem de codigo e nao do modelo.
```

### S07 — Base de conhecimento (RAG) · Sonnet

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/11-motor-do-agente.md (11.6),
      docs/spec/09-modelo-de-dados.md (knowledge_documents, knowledge_chunks).
Entregue a Sessao S07 de SESSIONS.md: app/knowledge/ingest.py (chunk ~500 tokens,
overlap 80, embeddings atras de interface EmbeddingProvider) e retrieve.py
(top-k=5, cosseno, filtro obrigatorio por tenant_id, corte de score minimo
configuravel; abaixo do corte retorna vazio).
Aceite: reingestao de um documento nao duplica chunks; busca com tenant A nunca
retorna chunk de B; consulta sem match retorna lista vazia (nao retorna lixo).
```

### S08 — Tools de leitura · Sonnet

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/11-motor-do-agente.md (11.4).
Entregue a Sessao S08 de SESSIONS.md: a infra de registro de tools (um arquivo por
tool com schema + handler + teste juntos) e as tools search_knowledge e
list_services (com filtro textual e matching por services.aliases).
Aceite: schema JSON valido; tenant_id nao aparece em nenhum input_schema;
list_services casa sinonimos configurados.
```

### S09 — Guardrails · Opus

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/11-motor-do-agente.md (11.5),
      docs/spec/10-configuracao-por-tenant.md (escalation),
      docs/spec/18-seguranca-e-lgpd.md, docs/spec/21-riscos-e-mitigacoes.md.
Entregue a Sessao S09 de SESSIONS.md: app/agent/guardrails.py com guardrails de
entrada (match de escalation.triggers por keyword/regex com curto-circuito,
rate limit, deteccao de prompt injection) e de saida (validacao de data/hora
contra os resultados de tool do turno, validacao de preco contra catalogo/RAG,
classificador de escopo clinico, quebra por max_message_chars, redacao de PII)
e o circuit breaker por custo/numero de mensagens.
Cuidados: esta e a defesa contra alucinacao de horario e preco — o risco de maior
impacto do projeto (secao 21). Cubra formatos de data em portugues por extenso,
"14h", "14:00", "amanha", "quarta que vem".
Aceite: bateria de testes de regex com pelo menos 20 formas de escrever horario;
resposta com horario nao vindo de tool e descartada e regenerada; segunda falha escala.
```

### S10 — Handoff · Sonnet

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/05-requisitos-funcionais.md (RF-26..RF-30),
      docs/spec/10-configuracao-por-tenant.md (escalation),
      docs/spec/12-fluxos-conversacionais.md (12.5).
Entregue a Sessao S10 de SESSIONS.md: app/agent/tools/escalate_to_human.py,
servico de handoff (abre handoffs, muda conversations.status, modo silencioso com
retomada por timeout handoff_silence_minutes), notificacao ao responsavel com
resumo e motivo, e audit_log de tudo com efeito externo.
Aceite: com handoff ativo o worker inbound nao chama o LLM; retomada por timeout
funciona; notificacao e enviada uma unica vez.
```

### S11 — Suíte conversacional · Opus

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/19-estrategia-de-testes.md (19.3, 19.4),
      docs/spec/12-fluxos-conversacionais.md.
Entregue a Sessao S11 de SESSIONS.md: runner pytest que executa cenarios YAML de
tests/conversational/ contra o agente com CalendarProvider e ChannelProvider falsos,
suportando as assercoes da 19.3 (handoff_opened, handoff_reason,
response_contains_any, no_tool_called, max_turns_to_escalate) mais juiz LLM opcional
para tom, e placar versionado que bloqueia queda de cenario.
Escreva os cenarios de escopo ja possiveis hoje (sem agendamento): preco, pergunta
sem resposta na base, emergencia, orientacao clinica, pedido de humano, hostil,
prompt injection.
Aceite: `pytest tests/conversational` roda offline e e deterministico nas assercoes
nao-LLM.
```

### S12 — CalendarProvider + Google · Opus

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/14-integracoes.md (14.3),
      docs/spec/09-modelo-de-dados.md (providers, resources).
Entregue a Sessao S12 de SESSIONS.md: app/calendar/base.py com o Protocol da 14.3,
app/calendar/google.py (OAuth com refresh token cifrado referenciado por
credentials_ref, freebusy.query agrupando ate 50 calendarios com cache de 60s,
events.insert com iCalUID = appointment_id, patch, delete, sync_changes),
retry com backoff em 403 rateLimitExceeded e 5xx, estado pending_sync em falha
persistente.
Aceite: testes com respx cobrindo freebusy, insert idempotente (mesmo iCalUID duas
vezes = 1 evento), 410 Gone no syncToken, 403 rateLimitExceeded.
```

### S13 — Motor de disponibilidade · Opus

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/13-motor-de-disponibilidade-e-agendamento.md (13.1-13.3),
      docs/spec/10-configuracao-por-tenant.md (scheduling, 10.4 business_hours),
      docs/spec/19-estrategia-de-testes.md (19.1).
Entregue a Sessao S13 de SESSIONS.md: app/scheduling/availability.py implementando
o algoritmo da 13.2 e app/scheduling/rules.py com as regras da 13.3.
Cuidados: toda aritmetica de data acontece na timezone da unidade e e convertida
para UTC na persistencia; nunca use datetime.now() sem tz — use o helper now_in(tz).
Horario de verao, virada de dia, jornada partida, feriado, buffer e recurso escasso
sao casos de teste obrigatorios.
Aceite: a bateria da 19.1 passa, incluindo servico mais longo que a janela de
trabalho (retorna vazio, nao estoura a janela).
```

### S14 — Hold, lock e confirmação · Opus

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/13-motor-de-disponibilidade-e-agendamento.md (13.4),
      docs/spec/09-modelo-de-dados.md (holds, appointments).
Entregue a Sessao S14 de SESSIONS.md: app/scheduling/booking.py com hold_slot
(transacao que verifica sobreposicao com appointments ativos e holds ativos),
pg_advisory_xact_lock por (provider_id, dia), confirmacao idempotente por
idempotency_key = hash(hold_id), traducao do erro da EXCLUDE constraint para
{"status": "slot_taken"}, e job de expiracao de holds.
Aceite: teste de concorrencia com testcontainers, 20 requisicoes simultaneas ao
mesmo slot, produz exatamente 1 appointment; repetir confirm com o mesmo hold_id
retorna o mesmo appointment_id sem criar outro.
```

### S15 — slot_token e tools de escrita · Sonnet

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/11-motor-do-agente.md (11.4).
Entregue a Sessao S15 de SESSIONS.md: slot_token assinado com HMAC carregando
provider_id, resource_id, service_id, starts_at e validade; e as tools
check_availability, hold_slot, confirm_appointment, find_appointments,
save_contact_info — cada uma com schema, handler e testes.
Aceite: slot_token adulterado e rejeitado; confirm_appointment com hold expirado
retorna {"status":"hold_expired"} com suggested_action; o modelo nao consegue
agendar um horario que nao veio de check_availability.
```

### S16 — Remarcação e cancelamento · Sonnet

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/12-fluxos-conversacionais.md (12.2, 12.3),
      docs/spec/05-requisitos-funcionais.md (RF-22).
Entregue a Sessao S16 de SESSIONS.md: tools reschedule_appointment e
cancel_appointment respeitando reschedule_min_notice_hours e
cancel_min_notice_hours (abaixo do minimo escala), remarcacao transacional
(cria novo, cancela antigo, atualiza evento; rollback + escalonamento em falha),
cancelamento com confirmacao explicita em dois turnos.
Aceite: cenarios conversacionais de remarcar/cancelar dentro e fora do prazo passam.
```

### S17 — Painel: conversas e takeover · Sonnet

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/15-painel-administrativo.md,
      docs/spec/18-seguranca-e-lgpd.md (18.1).
Entregue a Sessao S17 de SESSIONS.md: FastAPI + HTMX + Tailwind, mobile-first;
auth por link magico com papeis owner/attendant/support; caixa de conversas com
filtros; thread marcando agente vs humano; botoes Assumir/Devolver; CSRF, sessao
curta, rate limit no login; acesso support sempre em audit_log.
Aceite: assumir e devolver conversa sem perda de contexto; usuario de um tenant
nao enxerga dado de outro.
```

### S18 — Painel: cadastros · Sonnet

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/15-painel-administrativo.md,
      docs/spec/10-configuracao-por-tenant.md.
Entregue a Sessao S18 de SESSIONS.md: telas de Agenda (dia por profissional,
cancelar, remarcar, comparecimento/no-show), Servicos (CRUD com duracao, buffers,
preco, profissionais aptos), Horarios (jornada, excecoes, feriados), Base de
conhecimento (editor com reingestao automatica ao salvar) e Configuracao
(subconjunto seguro do YAML, validado pelo schema de S03).
Aceite: salvar um documento dispara reingestao; config invalida e recusada com o
campo apontado.
```

### S19 — Lembretes e templates utility · Opus

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/12-fluxos-conversacionais.md (12.6),
      docs/spec/14-integracoes.md (14.1.4), docs/spec/22-anexos.md (22.1).
Entregue a Sessao S19 de SESSIONS.md: cron de lembretes (send_at = starts_at +
offset, adiado para a proxima janela de business_hours se cair fora), verificacao
da janela de servico de 24h antes de qualquer envio proativo (aberta = texto livre;
fechada = template utility obrigatorio; sem template mapeado = nao envia e alerta),
registro de reminders.response e metrica de custo separando mensagem gratuita de paga.
Cuidados: este e o ponto onde um erro vira custo real e risco de bloqueio do numero.
Aceite: lembrete agendado para 3h da manha sai na abertura seguinte; com janela
fechada e template ausente, o envio e bloqueado e alertado.
```

### S20 — Sync reverso do Google · Opus

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/13-motor-de-disponibilidade-e-agendamento.md (13.5),
      docs/spec/14-integracoes.md (14.3), docs/spec/05-requisitos-funcionais.md (RF-23).
Entregue a Sessao S20 de SESSIONS.md: canal events.watch por profissional com
renovacao automatica, cron de reconciliacao a cada 15 min por syncToken com
fallback para full sync em 410 Gone, classificacao do evento (externo = so ocupacao;
correspondente a appointment movido/removido = atualiza status e notifica o contato;
conflito = registra e escala, nunca resolve sozinho).
Aceite: cancelamento feito direto no Google notifica o contato em ate 15 min;
token expirado recupera sozinho; evento externo nao vira appointment.
```

### S21 — Widget web · Sonnet

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/14-integracoes.md (14.2), docs/spec/07-arquitetura.md.
Entregue a Sessao S21 de SESSIONS.md: ChannelProvider web, endpoints (POST para
envio, SSE para streaming), Web Component vanilla em bundle unico com data-tenant,
sessao anonima em localStorage promovida a contact quando o telefone e informado,
CORS restrito a channels.web.allowed_origins, rate limit por IP.
Aceite: mesma engine e mesmas tools do WhatsApp; sem janela de 24h e sem template;
origem nao listada e recusada.
```

### S22 — Métricas · Sonnet

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/17-observabilidade-e-metricas.md.
Entregue a Sessao S22 de SESSIONS.md: tracing OpenTelemetry por conversa, log
estruturado de cada chamada de LLM (tokens, latencia, tools, motivo de guardrail),
as oito metricas de produto da 17.2 calculadas por tenant, tela de metricas no
painel e alertas (erro de webhook, fila crescendo, p95, falha do Google, custo
diario acima do teto).
Aceite: as oito metricas aparecem por tenant; alerta de custo dispara no teto.
```

### S23 — Onboarding de tenant · Haiku ou Sonnet

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/16-onboarding-de-um-novo-cliente-runbook.md,
      docs/spec/10-configuracao-por-tenant.md.
Entregue a Sessao S23 de SESSIONS.md: scripts/onboard_tenant.py criando tenant,
unidade, servicos e profissionais a partir de planilha padrao, com --validate e
--dry-run; a planilha modelo; e docs/RUNBOOK_ONBOARDING.md com os 10 passos e o
checklist de aceite do cliente.
Aceite: o script cria o tenant de exemplo do zero sem deploy; rodar duas vezes e
idempotente.
```

### S24 — Lista de espera, LGPD e agenda própria · Sonnet

```
Projeto: datamind-agenda.
Leia: CLAUDE.md, docs/spec/12-fluxos-conversacionais.md (12.7),
      docs/spec/18-seguranca-e-lgpd.md (18.2, 18.3), docs/spec/14-integracoes.md (14.4).
Entregue a Sessao S24 de SESSIONS.md: tool join_waitlist e job de encaixe (um
contato por vez, janela de 15 min, passa ao proximo sem resposta); retencao
configuravel com job de expurgo diario; endpoint de exclusao a pedido do titular
com anonimizacao preservando agregado; app/calendar/internal.py como segundo
CalendarProvider.
Aceite: encaixe oferece a um contato por vez; expurgo respeita os prazos por tenant;
internal.py passa na mesma bateria de testes de contrato do provider.
```

---

## Estado da Fase 0

Fechada e verificada contra Postgres real: 42 testes verdes (32 unitários + 10 de
integração), `ruff` e `mypy --strict` limpos, migrations `0001`–`0004` aplicadas.

O teste de isolamento pegou uma falha real durante a verificação: a aplicação conectava
como superusuário, e superusuário ignora RLS mesmo com `FORCE`. Corrigido com um papel
sem privilégio (`docs/DECISOES.md`, D-09). **A S04 já pode começar.**

Para conferir o ambiente antes de abrir uma sessão nova (PowerShell — `make` não existe
no Windows):

```powershell
.	asks.ps1 up
.	asks.ps1 check
```

---

## Convenções entre sessões

- Toda sessão termina com: testes verdes, `mypy --strict` limpo, `ruff` limpo e um commit
  citando os requisitos atendidos (`RF-xx`, `RNF-xx`).
- Se uma sessão precisar mudar algo entregue por outra, ela **atualiza os testes da outra**
  em vez de contornar.
- Descobertas que mudam o plano vão para `docs/DECISOES.md`, não para o código.
