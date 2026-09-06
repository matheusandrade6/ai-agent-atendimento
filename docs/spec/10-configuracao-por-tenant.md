## 10. Configuração por tenant

### 10.1 Princípio

Um arquivo YAML por tenant em `config/tenants/<slug>.yaml`, versionado em git, validado por Pydantic e materializado em `tenants.config` (JSONB) no deploy da config. **Toda personalização de cliente cabe aqui.** Se não couber, ou vira campo novo do schema, ou não é feito.

### 10.2 Schema (Pydantic, resumido)

```python
class TenantConfig(BaseModel):
    identity: Identity              # nome, vertical, descrição do negócio
    persona: Persona                # nome do agente, tom, tratamento, emojis, assinatura
    channels: Channels              # whatsapp, web
    scheduling: SchedulingRules
    intake: IntakeConfig
    escalation: EscalationConfig
    messages: MessageTemplates
    limits: Limits
```

### 10.3 Exemplo comentado (clínica veterinária)

```yaml
identity:
  name: "Clínica Veterinária Bicho Feliz"
  vertical: veterinaria
  about: >
    Clínica veterinária de bairro em Pinheiros, São Paulo. Atende cães e gatos.
    Consultas, vacinas, castração e exames laboratoriais. Não atende silvestres.

persona:
  agent_name: "Bia"
  introduce_as_ai: true            # exigência ética e de LGPD; ver 18.4
  tone: "cordial, direto, informal-profissional"
  address_form: "voce"             # voce | senhor_senhora
  emojis: sparingly
  max_message_chars: 600           # quebrar em mais de uma mensagem se passar
  signature: null
  vocabulary:
    prefer: ["tutor", "pet", "atendimento"]
    avoid: ["dono", "bichinho", "cliente"]

channels:
  whatsapp:
    phone_number_id: "${WA_PHONE_NUMBER_ID}"
    waba_id: "${WA_WABA_ID}"
    debounce_seconds: 6
  web:
    enabled: true
    allowed_origins: ["https://bichofeliz.com.br"]
    greeting: "Oi! Sou a Bia. Posso tirar dúvidas ou marcar um horário."

scheduling:
  calendar_provider: google
  min_lead_time_minutes: 120        # não oferece nada nas próximas 2h
  max_horizon_days: 45
  slots_per_offer: 3
  slot_granularity_minutes: 15
  hold_ttl_minutes: 10
  allow_reschedule: true
  reschedule_min_notice_hours: 4
  allow_cancel: true
  cancel_min_notice_hours: 4
  reminders:
    - kind: reminder_24h
      offset_hours: -24
      ask_confirmation: true
    - kind: reminder_2h
      offset_hours: -2
      ask_confirmation: false
  waitlist_enabled: true

intake:
  # campos coletados antes de propor horários
  required:
    - key: subject_name
      label: "nome do pet"
      type: string
    - key: species
      label: "espécie"
      type: enum
      options: [cachorro, gato]
      on_other: escalate            # espécie fora da lista -> handoff
    - key: is_first_visit
      label: "primeira vez na clínica"
      type: boolean
    - key: service
      label: "serviço"
      type: service_ref
    - key: contact_name
      label: "seu nome"
      type: string
  conditional:
    - when: "service.name contains 'vacina'"
      require:
        - key: vaccine_card
          label: "carteira de vacinação em dia"
          type: boolean
    - when: "is_first_visit == false"
      require:
        - key: last_visit_hint
          label: "quando foi a última visita"
          type: string
          optional: true
  ask_style: one_at_a_time          # one_at_a_time | grouped
  max_questions_before_offer: 5

escalation:
  business_hours_only: false
  notify:
    - channel: whatsapp
      to: "+5511999999999"
  triggers:
    - id: emergencia
      match_keywords: ["atropelado", "convulsão", "não respira", "sangramento",
                       "envenenado", "engasgado", "urgente", "emergência"]
      action: escalate_immediately
      reply: >
        Isso pode ser uma emergência. Vou chamar alguém da equipe agora.
        Se estiver muito grave, vá direto à clínica: {address}. Telefone: {phone}.
    - id: pedido_humano
      match_intent: ask_for_human
      action: escalate
    - id: orientacao_clinica
      match_intent: medical_advice
      action: refuse_and_offer_appointment
      reply: >
        Não consigo avaliar isso por aqui — quem faz isso é o veterinário
        na consulta. Quer que eu veja um horário?
    - id: baixa_confianca
      condition: "unanswered_questions >= 2"
      action: escalate
  handoff_silence_minutes: 120      # agente volta a responder após esse tempo

messages:
  greeting_new: >
    Oi! Aqui é a Bia, assistente virtual da Bicho Feliz 🐾
    Posso te ajudar com dúvidas ou marcar um horário. O que você precisa?
  greeting_returning: >
    Oi, {contact_name}! Que bom te ver de novo. Como posso ajudar?
  out_of_scope: >
    Essa eu não sei responder com segurança. Vou passar para alguém da equipe.
  confirmation: >
    Prontinho! ✅
    *{service}* para *{subject_name}*
    🗓 {date_human} às {time}
    👩‍⚕️ {provider}
    📍 {address}
    {price_line}
    {prep_line}
    Se precisar mudar, é só me chamar.

limits:
  max_llm_cost_usd_per_conversation: 0.15
  max_messages_per_conversation: 60
  monthly_budget_alert_usd: 60
```

### 10.4 Horários de funcionamento

```yaml
business_hours:
  timezone: America/Sao_Paulo
  weekly:
    mon: [{ start: "08:00", end: "12:00" }, { start: "13:30", end: "19:00" }]
    tue: [{ start: "08:00", end: "12:00" }, { start: "13:30", end: "19:00" }]
    wed: [{ start: "08:00", end: "12:00" }, { start: "13:30", end: "19:00" }]
    thu: [{ start: "08:00", end: "12:00" }, { start: "13:30", end: "19:00" }]
    fri: [{ start: "08:00", end: "12:00" }, { start: "13:30", end: "18:00" }]
    sat: [{ start: "09:00", end: "13:00" }]
    sun: []
  exceptions:
    - date: "2026-12-25"
      closed: true
    - date: "2026-12-24"
      windows: [{ start: "09:00", end: "12:00" }]
```

---
