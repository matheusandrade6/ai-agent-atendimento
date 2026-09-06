## 22. Anexos

### 22.1 Templates WhatsApp a submeter (categoria utility)

| Nome | Corpo | Variáveis |
|---|---|---|
| `lembrete_24h` | "Oi {{1}}! Lembrando do seu horário na {{2}}: {{3}} às {{4}}. Confirma presença? Responda SIM ou NÃO." | nome, estabelecimento, data, hora |
| `confirmacao_agendamento` | "Agendamento confirmado: {{1}} em {{2}} às {{3}} com {{4}}. Endereço: {{5}}." | serviço, data, hora, profissional, endereço |
| `cancelamento_prestador` | "Precisamos remarcar seu horário de {{1}} às {{2}}. Me chame para escolher outro dia." | data, hora |
| `retomada_conversa` | "Oi {{1}}, ficamos com uma pendência no seu agendamento. Posso ajudar?" | nome |

### 22.2 Deltas de configuração por vertical

**Salão de beleza** — combo de serviços na mesma visita:

```yaml
scheduling:
  allow_service_bundle: true        # soma durações, aloca no mesmo profissional
intake:
  required:
    - { key: service, type: service_ref, allow_multiple: true }
    - { key: preferred_professional, type: provider_ref, optional: true }
    - { key: hair_length, label: "comprimento do cabelo", type: enum,
        options: [curto, medio, longo], affects_duration: true }
```

**Clínica médica** — convênio e tipo de consulta:

```yaml
intake:
  required:
    - { key: is_first_visit, type: boolean }
    - { key: payment_type, type: enum, options: [particular, convenio] }
  conditional:
    - when: "payment_type == 'convenio'"
      require:
        - { key: insurance_name, type: enum_ref, source: accepted_insurances,
            on_other: escalate }
escalation:
  triggers:
    - id: sintoma_grave
      match_keywords: ["dor no peito", "falta de ar", "desmaio", "sangramento"]
      action: escalate_immediately
      reply: "Isso precisa de atendimento imediato. Procure um pronto-socorro ou ligue 192."
```

**Fisioterapeuta** — sessões recorrentes:

```yaml
scheduling:
  allow_recurring: true
  recurrence_max_sessions: 10
  recurrence_pattern: weekly_same_slot
```

### 22.3 Convenções de código

- Type hints obrigatórios; `mypy --strict` no CI.
- Nada de `datetime.now()` sem timezone. Helper único `now_in(tz)`.
- Nenhuma query sem `tenant_id` — lint customizado que falha o CI.
- Tools: um arquivo por tool, com schema, handler e testes juntos.
- Prompts em `app/agent/prompt.py` como blocos nomeados, nunca strings soltas no meio da lógica.
- Migrations sempre reversíveis.

---
