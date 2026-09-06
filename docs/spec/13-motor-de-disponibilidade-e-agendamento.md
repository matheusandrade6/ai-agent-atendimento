## 13. Motor de disponibilidade e agendamento

### 13.1 Entradas

`service_id`, janela `[date_from, date_to]`, `provider_id` opcional, preferências, `now`.

### 13.2 Algoritmo

```
Para cada provider apto ao serviço (service_providers):
  1. Gerar janelas de trabalho por dia na timezone da unidade:
     working_hours ∩ business_hours − exceções
  2. Buscar ocupação:
     a. Google Calendar freeBusy(calendar_id, timeMin, timeMax)
     b. appointments ativos no banco (redundância defensiva)
     c. holds ativos não expirados
  3. duration_total = duration + buffer_before + buffer_after
     (duração pode ser sobrescrita por service_providers.duration_override_min)
  4. Varrer cada janela em passos de slot_granularity_minutes,
     candidato válido se [t, t+duration_total] ⊆ janela e não intersecta ocupação
  5. Filtrar: t >= now + min_lead_time_minutes
              t <= now + max_horizon_days
  6. Se o serviço exige recurso, aplicar a mesma checagem ao calendário/ocupação
     do recurso; slot só é válido se profissional E recurso estiverem livres
Unir slots de todos os providers, deduplicar por horário,
ordenar por (aderência às preferências, proximidade temporal),
truncar em `limit`, assinar slot_token.
```

### 13.3 Regras de negócio a respeitar

| Regra | Origem |
|---|---|
| Antecedência mínima | `scheduling.min_lead_time_minutes` |
| Horizonte máximo | `scheduling.max_horizon_days` |
| Buffers antes/depois | `services` |
| Duração por profissional | `service_providers.duration_override_min` |
| Almoço/intervalos | lacunas nas janelas de `working_hours` |
| Feriados e exceções | `business_hours.exceptions` |
| Recurso escasso | `service_resources` |
| Serviço online | ignora `location`, usa link de videochamada no evento |

### 13.4 Concorrência e integridade

Três camadas, em ordem:

1. **Hold com TTL.** Antes de confirmar, `hold_slot` grava linha em `holds` dentro de transação que verifica ausência de sobreposição com `appointments` ativos e `holds` ativos. Job de cron expira holds vencidos.
2. **Advisory lock.** `pg_advisory_xact_lock(hashtext(provider_id::text || date))` durante hold e confirmação, serializando escritas por profissional/dia.
3. **Constraint de exclusão.** `appt_no_overlap` (seção 9) é a rede final: mesmo com bug de lógica, o banco recusa overbooking. Erro de constraint na confirmação é traduzido para `{"status": "slot_taken"}` e o agente reoferece.

**Idempotência:** `confirm_appointment` usa `idempotency_key = hash(hold_id)`. Repetição retorna o agendamento existente, não cria outro. A criação do evento no Google usa o mesmo `appointment_id` como `iCalUID` para tolerar retry.

### 13.5 Sincronização reversa (RF-23)

- **Push:** canal de notificação `events.watch` do Google Calendar por profissional, com renovação automática antes do vencimento.
- **Pull:** cron de reconciliação a cada 15 min usando `syncToken` incremental por profissional; fallback para full sync quando o token expira (`410 Gone`).
- Evento externo criado direto no Google vira ocupação (não vira `appointment`).
- Evento correspondente a um `appointment` que foi movido ou removido no Google → atualiza status no banco, notifica o contato pelo canal e, se removido, oferece remarcação.
- Conflito (o prestador criou algo em cima de um agendamento do agente): registra, escala ao atendente. Não resolve sozinho.

---
