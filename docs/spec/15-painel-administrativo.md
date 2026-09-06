## 15. Painel administrativo

Escopo v1, mobile-first, server-rendered:

| Tela | Conteúdo |
|---|---|
| Caixa de conversas | Lista com filtro (ativas, em handoff, não lidas). Abertura mostra a thread completa com marcação do que foi agente vs humano. Botão **Assumir** silencia o agente; **Devolver** retoma. |
| Agenda | Lista/dia dos agendamentos por profissional, com status e origem. Ações: cancelar, remarcar, marcar comparecimento/no-show. |
| Serviços | CRUD de serviços, duração, buffers, preço, profissionais aptos. |
| Horários | Jornada por profissional, exceções, feriados. |
| Base de conhecimento | Editor de documentos com reingestão automática. FAQ em formato pergunta/resposta. |
| Configuração | Persona, mensagens, gatilhos de escalonamento, lembretes. Subconjunto seguro do YAML, com validação. |
| Métricas | Conversas, taxa de agendamento, taxa de handoff, no-show, custo (seção 17). |

Autenticação: e-mail + link mágico. Papéis: `owner`, `attendant`. Datamind acessa com papel `support` e todo acesso vai para `audit_log`.

---
