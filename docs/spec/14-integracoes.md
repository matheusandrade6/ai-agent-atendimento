## 14. Integrações

### 14.1 WhatsApp Cloud API

#### 14.1.1 Ingestão

- `GET /webhooks/whatsapp` — verificação do `hub.challenge`.
- `POST /webhooks/whatsapp` — validar `X-Hub-Signature-256` (HMAC SHA256 com o app secret) **antes de qualquer parsing**. Assinatura inválida → 403.
- Deduplicar por `messages[].id` (constraint `UNIQUE (tenant_id, provider_msg_id)`).
- Responder **200 em menos de 1s**, sempre, mesmo com erro interno. Processamento vai para a fila. A Meta reentrega em caso de não-200 e isso gera duplicidade.
- Roteamento multi-tenant: `entry[].changes[].value.metadata.phone_number_id` → tenant.

#### 14.1.2 Agregação (debounce)

Chave Redis `agg:{conversation_id}`; cada mensagem reinicia o timer de `debounce_seconds`; ao expirar, concatena o texto na ordem e dispara um único turno do agente. Evita responder três vezes a uma pessoa que escreveu em três balões.

#### 14.1.3 Envio

- Texto livre, marcação de leitura e indicador de digitação enquanto processa.
- Fila de saída com backoff exponencial; respeitar rate limit e `429`.
- Persistir `provider_msg_id` e atualizar `status` pelos webhooks de `statuses` (sent/delivered/read/failed).

#### 14.1.4 Janela de serviço e templates (regra de custo — importante)

Desde 1º de julho de 2025 a Meta cobra **por mensagem**, não por conversa. Categorias e implicações:

| Categoria | Custo | Uso no produto |
|---|---|---|
| **Service** (resposta dentro da janela de 24h aberta pelo contato) | gratuito | Todo o atendimento reativo — o grosso do tráfego. |
| **Utility** (template transacional fora da janela) | pago, tarifa baixa | Lembrete, confirmação, aviso de cancelamento pelo prestador. |
| **Marketing** | pago, tarifa alta | Fora do escopo v1. |
| **Authentication** | pago, tarifa baixa | Não usado. |

Consequências de implementação:

- `conversations.service_window_expires_at` é atualizado a cada mensagem **inbound**.
- Antes de enviar qualquer mensagem proativa, o `ChannelProvider` verifica a janela. Aberta → texto livre. Fechada → **obrigatoriamente** template utility aprovado.
- Templates aprovados por tenant ficam em `channels.whatsapp.templates` na config, com nome, idioma e ordem de variáveis. Nenhum envio proativo sem template mapeado.
- Métrica de custo por tenant separa mensagens gratuitas de pagas.

#### 14.1.5 Interface

```python
class ChannelProvider(Protocol):
    async def send_text(self, conversation: Conversation, text: str) -> SentMessage: ...
    async def send_template(self, conversation: Conversation,
                            template: str, variables: dict) -> SentMessage: ...
    async def mark_read(self, provider_msg_id: str) -> None: ...
    async def download_media(self, media_id: str) -> MediaFile: ...
    def service_window_open(self, conversation: Conversation) -> bool: ...
```

### 14.2 Widget web

- Web Component distribuído como `<script src="https://cdn.datamind.../widget.js" data-tenant="slug">`.
- Sessão anônima com id em `localStorage`; vira `contact` quando o telefone é informado.
- Transporte: SSE para streaming da resposta, POST para envio.
- CORS restrito a `channels.web.allowed_origins`.
- Rate limit por IP + captcha invisível após N mensagens.
- **Mesma engine**, mesmas tools. Diferenças no adaptador: sem janela de 24h, sem templates, sem debounce agressivo (default 2s).

### 14.3 Google Calendar

- **OAuth 2.0** com consentimento do cliente durante o onboarding; escopo `https://www.googleapis.com/auth/calendar`. Refresh token cifrado em secret store, referenciado por `providers.credentials_ref`.
- **Leitura de ocupação:** `freebusy.query` com `timeMin`/`timeMax`/`items[calendarId]`, agrupando até 50 calendários por chamada. Cache curto (60s) por profissional/dia.
- **Escrita:** `events.insert` com `iCalUID = appointment_id`, `summary` padronizado (`{serviço} — {subject_name}`), `description` com resumo do intake e link para a conversa no painel, `attendees` opcional (`sendUpdates: none` para não duplicar notificação), `extendedProperties.private = {datamind_appointment_id, datamind_tenant}`.
- **Atualização/cancelamento:** `events.patch` / `events.delete`.
- **Sync:** `events.watch` (push) + `events.list` com `syncToken` (pull), conforme 13.5.
- **Resiliência:** retry com backoff em `403 rateLimitExceeded` e `5xx`; falha persistente → agendamento fica `pending_sync`, alerta ao operador, agente informa que confirmará em instantes e escala se não resolver.

```python
class CalendarProvider(Protocol):
    async def get_busy(self, calendar_ids: list[str],
                       start: datetime, end: datetime) -> dict[str, list[Interval]]: ...
    async def create_event(self, calendar_id: str, event: EventDraft,
                           idempotency_key: str) -> ExternalEvent: ...
    async def update_event(self, calendar_id: str, event_id: str,
                           patch: EventPatch) -> ExternalEvent: ...
    async def delete_event(self, calendar_id: str, event_id: str) -> None: ...
    async def sync_changes(self, calendar_id: str,
                           sync_token: str | None) -> SyncResult: ...
```

### 14.4 Integrações verticais (previsto, não implementado)

`CalendarProvider` + um futuro `PracticeManagementProvider` (paciente, prontuário, faturamento). Nenhuma implementação em v1, mas nenhum acoplamento a Google fora de `calendar/google.py`.

---
