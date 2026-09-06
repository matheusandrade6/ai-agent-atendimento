## 21. Riscos e mitigações

| Risco | Impacto | Mitigação |
|---|---|---|
| Alucinação de horário ou preço | Alto — quebra de confiança e prejuízo real | Guardrail de saída obrigatório + `slot_token` assinado + suíte de regressão |
| Overbooking | Alto | Três camadas (hold, advisory lock, constraint de exclusão) |
| Prestador ignora o sistema e usa só o Google | Médio | Sync bidirecional; agenda Google é fonte de verdade da ocupação |
| Aprovação de template WhatsApp demorada | Médio — trava lembretes | Iniciar no passo 5 do onboarding; ter fallback de lembrete só dentro da janela |
| Custo de LLM acima do previsto | Médio | Teto por conversa, resumo rolante, modelo menor para extração e classificação |
| Conteúdo clínico inadequado | Alto — regulatório | Recusa explícita no prompt + guardrail + escalonamento; nunca opinar |
| Vazamento entre tenants | Crítico | RLS + testes automatizados de isolamento em CI |
| Bloqueio do número no WhatsApp | Alto | Sem marketing não solicitado, respeitar opt-out, monitorar quality rating |
| Cliente pede feature exclusiva | Médio — vira colcha de retalhos | Regra de ouro da seção 0: config ou feature genérica com flag |

---
