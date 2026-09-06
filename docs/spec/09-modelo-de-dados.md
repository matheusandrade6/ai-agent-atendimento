## 9. Modelo de dados

DDL de referência (simplificado; índices e constraints essenciais incluídos).

```sql
-- ============ TENANCY ============
CREATE TABLE tenants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug            TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    vertical        TEXT NOT NULL,           -- veterinaria | medica | estetica | ...
    status          TEXT NOT NULL DEFAULT 'active',
    config          JSONB NOT NULL,          -- ver seção 10
    config_version  INT  NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE locations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    name        TEXT NOT NULL,
    timezone    TEXT NOT NULL,               -- IANA, ex. America/Sao_Paulo
    address     JSONB,
    business_hours JSONB NOT NULL,           -- ver seção 10.4
    active      BOOLEAN NOT NULL DEFAULT true
);

-- ============ OFERTA ============
CREATE TABLE providers (                     -- profissionais
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    location_id     UUID REFERENCES locations(id),
    name            TEXT NOT NULL,
    display_name    TEXT,
    role            TEXT,
    working_hours   JSONB NOT NULL,          -- override do business_hours
    calendar_provider TEXT NOT NULL DEFAULT 'google',
    calendar_id     TEXT,                    -- id da agenda no provider
    credentials_ref TEXT,                    -- ponteiro p/ secret store
    sync_token      TEXT,                    -- incremental sync do Google
    active          BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE resources (                     -- salas, cadeiras, equipamentos
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    location_id UUID REFERENCES locations(id),
    name        TEXT NOT NULL,
    capacity    INT NOT NULL DEFAULT 1,
    calendar_id TEXT
);

CREATE TABLE services (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NOT NULL REFERENCES tenants(id),
    name               TEXT NOT NULL,
    aliases            TEXT[] NOT NULL DEFAULT '{}',   -- sinônimos p/ matching
    description        TEXT,
    duration_minutes   INT NOT NULL,
    buffer_before_min  INT NOT NULL DEFAULT 0,
    buffer_after_min   INT NOT NULL DEFAULT 0,
    price_cents        INT,
    price_note         TEXT,                            -- "a partir de", "sob avaliação"
    modality           TEXT NOT NULL DEFAULT 'in_person', -- in_person | online
    requires_intake    JSONB NOT NULL DEFAULT '[]',     -- campos extras obrigatórios
    prep_instructions  TEXT,
    active             BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE service_providers (
    service_id  UUID REFERENCES services(id),
    provider_id UUID REFERENCES providers(id),
    duration_override_min INT,
    PRIMARY KEY (service_id, provider_id)
);

CREATE TABLE service_resources (
    service_id  UUID REFERENCES services(id),
    resource_id UUID REFERENCES resources(id),
    PRIMARY KEY (service_id, resource_id)
);

-- ============ DEMANDA ============
CREATE TABLE contacts (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id),
    phone_e164    TEXT,
    name          TEXT,
    email         TEXT,
    external_ref  TEXT,
    attributes    JSONB NOT NULL DEFAULT '{}',
    consent       JSONB NOT NULL DEFAULT '{}',   -- LGPD: base legal, data, canal
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, phone_e164)
);

CREATE TABLE subjects (                     -- quem recebe o atendimento
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    contact_id  UUID NOT NULL REFERENCES contacts(id),
    kind        TEXT NOT NULL DEFAULT 'self', -- self | dependent | pet
    name        TEXT,
    attributes  JSONB NOT NULL DEFAULT '{}'   -- espécie, raça, idade, convênio...
);

-- ============ CONVERSA ============
CREATE TABLE conversations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    contact_id      UUID NOT NULL REFERENCES contacts(id),
    channel         TEXT NOT NULL,            -- whatsapp | web
    channel_thread  TEXT NOT NULL,            -- wa_id ou session id
    status          TEXT NOT NULL DEFAULT 'active', -- active | handoff | closed
    stage           TEXT NOT NULL DEFAULT 'greeting',
    collected       JSONB NOT NULL DEFAULT '{}',    -- campos de intake extraídos
    summary         TEXT,                            -- resumo rolante
    service_window_expires_at TIMESTAMPTZ,           -- janela 24h WhatsApp
    last_message_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, channel, channel_thread)
);

CREATE TABLE messages (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id),
    conversation_id  UUID NOT NULL REFERENCES conversations(id),
    direction        TEXT NOT NULL,           -- inbound | outbound
    author           TEXT NOT NULL,           -- contact | agent | human
    content_type     TEXT NOT NULL DEFAULT 'text',
    content          TEXT,
    media_ref        TEXT,
    provider_msg_id  TEXT,                    -- id no canal (dedup)
    status           TEXT,                    -- sent | delivered | read | failed
    tool_calls       JSONB,
    tokens_in        INT,
    tokens_out       INT,
    cost_usd         NUMERIC(10,6),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, provider_msg_id)
);

-- ============ AGENDAMENTO ============
CREATE TABLE holds (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    conversation_id UUID NOT NULL REFERENCES conversations(id),
    provider_id     UUID NOT NULL REFERENCES providers(id),
    resource_id     UUID REFERENCES resources(id),
    service_id      UUID NOT NULL REFERENCES services(id),
    starts_at       TIMESTAMPTZ NOT NULL,
    ends_at         TIMESTAMPTZ NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active'   -- active | consumed | expired
);
CREATE INDEX ON holds (tenant_id, provider_id, starts_at, ends_at)
    WHERE status = 'active';

CREATE TABLE appointments (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id),
    contact_id       UUID NOT NULL REFERENCES contacts(id),
    subject_id       UUID REFERENCES subjects(id),
    conversation_id  UUID REFERENCES conversations(id),
    provider_id      UUID NOT NULL REFERENCES providers(id),
    resource_id      UUID REFERENCES resources(id),
    service_id       UUID NOT NULL REFERENCES services(id),
    location_id      UUID REFERENCES locations(id),
    starts_at        TIMESTAMPTZ NOT NULL,
    ends_at          TIMESTAMPTZ NOT NULL,
    status           TEXT NOT NULL DEFAULT 'confirmed',
        -- confirmed | rescheduled | cancelled | no_show | completed
    source           TEXT NOT NULL DEFAULT 'agent',   -- agent | human | external
    external_event_id TEXT,                            -- id no Google Calendar
    idempotency_key  TEXT,
    intake           JSONB NOT NULL DEFAULT '{}',
    cancellation     JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key)
);
-- Não-sobreposição por profissional entre agendamentos ativos:
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE appointments ADD CONSTRAINT appt_no_overlap
    EXCLUDE USING gist (
        provider_id WITH =,
        tstzrange(starts_at, ends_at) WITH &&
    ) WHERE (status IN ('confirmed','rescheduled'));

CREATE TABLE reminders (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id),
    appointment_id UUID NOT NULL REFERENCES appointments(id),
    kind           TEXT NOT NULL,      -- reminder_24h | reminder_2h | followup
    send_at        TIMESTAMPTZ NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    response       TEXT                -- confirmed | cancelled | no_reply
);

CREATE TABLE waitlist (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id),
    contact_id   UUID NOT NULL REFERENCES contacts(id),
    service_id   UUID NOT NULL REFERENCES services(id),
    provider_id  UUID REFERENCES providers(id),
    preferences  JSONB NOT NULL DEFAULT '{}',
    expires_at   TIMESTAMPTZ,
    status       TEXT NOT NULL DEFAULT 'active'
);

-- ============ CONHECIMENTO ============
CREATE TABLE knowledge_documents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    title       TEXT NOT NULL,
    source      TEXT,
    content     TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE knowledge_chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    document_id UUID NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    embedding   VECTOR(1536)
);
CREATE INDEX ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);

-- ============ OPERAÇÃO ============
CREATE TABLE handoffs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    conversation_id UUID NOT NULL REFERENCES conversations(id),
    reason          TEXT NOT NULL,
    triggered_by    TEXT NOT NULL,     -- agent | contact | rule
    assigned_to     TEXT,
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at       TIMESTAMPTZ
);

CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   UUID NOT NULL,
    actor       TEXT NOT NULL,          -- agent | user:<id> | system
    action      TEXT NOT NULL,
    entity      TEXT,
    entity_id   UUID,
    payload     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**RLS:** todas as tabelas com `tenant_id` recebem `ENABLE ROW LEVEL SECURITY` e política `USING (tenant_id = current_setting('app.tenant_id')::uuid)`. A sessão de banco define `app.tenant_id` no início de cada request/job.

---
