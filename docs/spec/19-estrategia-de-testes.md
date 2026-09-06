## 19. Estratégia de testes

### 19.1 Unitários

- Motor de disponibilidade: cobrir horário de verão, virada de dia, buffers, exceções, recurso escasso, jornada partida, serviço mais longo que a janela.
- Parsing de config e validação do YAML.
- Guardrails de saída (regex de data/preço).

### 19.2 Integração

- Webhook WhatsApp: assinatura inválida, payload duplicado, payload malformado, status callbacks.
- Google Calendar mockado via `respx`: freebusy, insert idempotente, `410 Gone` no sync token, `403 rateLimitExceeded`.
- Concorrência: duas conversas disputando o mesmo slot → exatamente um agendamento (teste com `testcontainers` e transações paralelas).
- Isolamento: consulta com `tenant_id` A jamais retorna linha de B.

### 19.3 Conversacional (suíte de cenários)

Arquivos YAML em `tests/conversational/`, executados contra o agente com calendário e canal falsos, avaliando por asserções determinísticas + juiz LLM para tom.

```yaml
scenario: emergencia_veterinaria
tenant: clinica-exemplo
turns:
  - user: "socorro meu cachorro foi atropelado"
    expect:
      handoff_opened: true
      handoff_reason: emergencia
      response_contains_any: ["equipe", "clínica"]
      no_tool_called: [check_availability, confirm_appointment]
      max_turns_to_escalate: 1
```

Cenários obrigatórios (mínimo 25, por tenant): agendamento feliz; agendamento com preferência impossível; pergunta de preço; pergunta sem resposta na base; emergência; pedido de orientação clínica; pedido de humano; remarcação; cancelamento dentro e fora do prazo; cliente muda de ideia no meio; cliente manda tudo numa mensagem só; cliente manda áudio; cliente hostil; tentativa de prompt injection; slot tomado entre a oferta e a confirmação; duas conversas simultâneas; cliente fora da área atendida; serviço inexistente; menor de idade/dependente; cliente recorrente.

### 19.4 Regressão

Toda alteração de prompt roda a suíte inteira. Placar de aprovação por cenário versionado — queda em qualquer cenário bloqueia o merge.

---
