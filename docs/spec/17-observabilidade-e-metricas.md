## 17. Observabilidade e métricas

### 17.1 Técnicas

- Trace por conversa: mensagem → agente → tools → envio.
- Log estruturado de cada chamada de LLM: tokens, latência, tools acionadas, motivo de guardrail.
- Alertas: taxa de erro de webhook, fila crescendo, latência p95, falha de Google Calendar, custo diário por tenant acima do teto.

### 17.2 De produto (por tenant, semanais)

| Métrica | Definição | Meta v1 |
|---|---|---|
| Taxa de resolução autônoma | conversas encerradas sem handoff / total | > 70% |
| Taxa de conversão | conversas com agendamento / conversas com intenção de agendar | > 55% |
| Tempo até primeira resposta | p95 | < 5s |
| Turnos até agendamento | mediana | ≤ 8 |
| Taxa de handoff por motivo | distribuição | emergência ≠ falha |
| No-show | agendamentos com `no_show` / total | reduzir vs baseline |
| Custo por conversa | LLM + WhatsApp pago | < US$ 0,10 |
| Alucinação detectada | acionamentos do guardrail de saída | → 0 |

---
