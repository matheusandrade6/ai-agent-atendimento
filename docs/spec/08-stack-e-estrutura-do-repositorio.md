## 8. Stack e estrutura do repositório

### 8.1 Stack

| Camada | Escolha |
|---|---|
| Linguagem | Python 3.12 |
| API | FastAPI + Uvicorn |
| Workers/filas | ARQ (Redis) — jobs assíncronos e cron |
| Banco | PostgreSQL 16 + extensão `pgvector` |
| Cache/estado efêmero | Redis (buffer de debounce, locks distribuídos, rate limit) |
| ORM/migrations | SQLAlchemy 2.x + Alembic |
| LLM | Anthropic Messages API com tool use, atrás de interface `LLMProvider` |
| Transcrição de áudio | Whisper API (ou equivalente), atrás de interface `SpeechProvider` |
| Validação | Pydantic v2 (config de tenant, payloads, args de tools) |
| Painel | FastAPI + HTMX + Tailwind (v1) — sem SPA |
| Widget | Web Component vanilla, bundle único `<script>` |
| Observabilidade | OpenTelemetry + Logfire/Grafana; logs estruturados JSON |
| Testes | pytest, pytest-asyncio, respx, testcontainers |
| Deploy | Docker + Fly.io/Render (v1); Postgres gerenciado |

### 8.2 Estrutura do repositório

```
datamind-agenda/
├── app/
│   ├── api/
│   │   ├── webhooks/          # whatsapp.py, google_calendar.py
│   │   ├── widget/            # endpoints do chat web
│   │   └── admin/             # painel
│   ├── core/
│   │   ├── config.py          # settings da aplicação
│   │   ├── security.py        # assinatura de webhook, auth do painel
│   │   ├── db.py              # sessão, RLS, tenant context
│   │   └── telemetry.py
│   ├── domain/
│   │   ├── models.py          # SQLAlchemy
│   │   ├── schemas.py         # Pydantic
│   │   └── tenant_config.py   # schema da config + loader
│   ├── channels/
│   │   ├── base.py            # ChannelProvider (interface)
│   │   ├── whatsapp.py
│   │   └── web.py
│   ├── calendar/
│   │   ├── base.py            # CalendarProvider (interface)
│   │   ├── google.py
│   │   └── internal.py        # stub p/ agenda própria (Fase 4)
│   ├── agent/
│   │   ├── engine.py          # loop de tool calling
│   │   ├── prompt.py          # montagem do system prompt
│   │   ├── tools/             # uma tool por arquivo
│   │   ├── guardrails.py
│   │   └── memory.py          # resumo rolante, extração de campos
│   ├── scheduling/
│   │   ├── availability.py    # motor de slots
│   │   ├── booking.py         # hold, confirm, reschedule, cancel
│   │   └── rules.py           # políticas do tenant
│   ├── knowledge/
│   │   ├── ingest.py          # chunking + embeddings
│   │   └── retrieve.py
│   ├── workers/
│   │   ├── inbound.py         # processa mensagem recebida
│   │   ├── outbound.py        # envia mensagem
│   │   └── cron.py            # lembretes, expiração de hold, sync de calendário
│   └── main.py
├── config/
│   └── tenants/
│       ├── _schema.yaml       # contrato
│       ├── _template.yaml     # base para novo cliente
│       └── clinica-exemplo.yaml
├── migrations/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── conversational/        # cenários de conversa (seção 19.3)
├── scripts/
│   └── onboard_tenant.py      # runbook automatizado (seção 16)
└── docker-compose.yml
```

---
