## 12. Fluxos conversacionais

### 12.1 Fluxo principal — primeiro contato até agendamento

```
Cliente: oi, vcs atendem gato? quanto é a consulta?
  → guardrails: sem gatilho
  → RAG: "atendemos cães e gatos"; catálogo: Consulta clínica R$ 180, 30min
Agente: Oi! Aqui é a Bia, assistente virtual da Bicho Feliz 🐾
        Atendemos sim, cães e gatos. A consulta clínica é R$ 180 e dura
        cerca de 30 minutos. Quer que eu veja um horário?

Cliente: quero sim
  → stage: qualifying | missing: [subject_name, species, is_first_visit, contact_name]
Agente: Perfeito. Qual o nome do seu gato?

Cliente: Mia, ela nunca foi aí
  → extração: subject_name=Mia, species=gato, is_first_visit=true
Agente: Anotado, Mia 🐱 E como você se chama?

Cliente: Carla
  → intake completo
Agente: Obrigada, Carla! Tem preferência de dia ou horário?

Cliente: de tarde, essa semana se der
  → check_availability(service=consulta, date_from=hoje, date_to=+7d,
                       preferred_periods=[afternoon])
Agente: Tenho esses horários à tarde:
        1️⃣ Terça, 08/09, às 14h30
        2️⃣ Quarta, 09/09, às 16h
        3️⃣ Sexta, 11/09, às 15h
        Algum funciona?

Cliente: o de quarta
  → hold_slot(slot_token do item 2)
  → confirm_appointment(hold_id, subject_name=Mia, intake={...})
Agente: Prontinho! ✅
        *Consulta clínica* para *Mia*
        🗓 quarta-feira, 9 de setembro, às 16h
        👩‍⚕️ Dra. Ana
        📍 Rua Exemplo, 123 — Pinheiros
        💰 R$ 180
        📋 Leve a carteira de vacinação se tiver.
        Se precisar mudar, é só me chamar.
```

### 12.2 Remarcação

Gatilho: "preciso mudar", "não vou conseguir", "dá pra passar pra outro dia".
1. `find_appointments` → se mais de um, pede desambiguação.
2. Verifica `reschedule_min_notice_hours`. Abaixo do mínimo → escala.
3. `check_availability` para o mesmo serviço.
4. `reschedule_appointment` — transacional: cria o novo, cancela o antigo, atualiza o evento no calendário. Se qualquer passo falhar, rollback e escalonamento.

### 12.3 Cancelamento

1. `find_appointments`, confirma qual.
2. Confirmação explícita ("Confirma o cancelamento de X no dia Y?") — nunca cancela em uma mensagem só.
3. `cancel_appointment` → status `cancelled`, evento removido/marcado no calendário, motivo registrado.
4. Se `waitlist_enabled`, dispara o job de encaixe.
5. Oferece reagendar.

### 12.4 Dúvida sem agendamento

Responde pelo RAG, encerra sem forçar agendamento. Uma única oferta suave ao final ("Se quiser, posso ver um horário"). Nunca insiste após recusa.

### 12.5 Fora de escopo e handoff

```
Cliente: minha gata tá vomitando desde ontem, o que pode ser?
  → guardrail de entrada: intent = medical_advice
Agente: Não consigo avaliar isso por aqui — quem faz isso é a veterinária
        na consulta. Vômito recorrente merece ser visto. Quer que eu procure
        o horário mais próximo?
```

```
Cliente: minha cachorra foi atropelada agora
  → guardrail: trigger "atropelado" → escalate_immediately
Agente: Isso pode ser uma emergência. Vou chamar alguém da equipe agora.
        Se estiver muito grave, vá direto à clínica: Rua Exemplo, 123.
        Telefone: (11) 3333-3333.
  → handoff aberto + notificação WhatsApp ao responsável
  → agente em silêncio
```

### 12.6 Lembrete e confirmação

- `send_at = starts_at - 24h`, respeitando `business_hours` (não envia de madrugada; adia para a próxima janela).
- Se a janela de serviço de 24h estiver **fechada**, o envio usa **template utility aprovado** (seção 14.1.4).
- Resposta "sim/confirmo" → `reminders.response = confirmed`. "não/não vou" → entra no fluxo de cancelamento. Sem resposta → `no_reply`, registrado para métrica de no-show.

### 12.7 Lista de espera e encaixe (Fase 3)

Ao vagar um slot, o job busca `waitlist` compatível (serviço, profissional, preferências, ordem de entrada), oferece a **um contato por vez** com janela de resposta de 15 min, e passa ao próximo se não houver resposta.

---
