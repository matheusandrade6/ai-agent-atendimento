## 16. Onboarding de um novo cliente (runbook)

Meta: ≤ 4 horas de trabalho, sem deploy (RNF-10).

1. **Entrevista de descoberta (60 min).** Roteiro fixo: serviços e preços, duração real de cada um, profissionais e jornadas, política de cancelamento, convênios/pagamento, as 20 perguntas mais frequentes, vocabulário da casa, o que **nunca** pode ser dito, quem recebe escalonamento e em que horário.
2. **Coleta de ativos.** Site, cardápio de serviços, tabela de preços, textos que já usam no WhatsApp (fonte principal de tom de voz).
3. **Criação do tenant.** `python scripts/onboard_tenant.py --slug ... --vertical ...` cria tenant, unidade, serviços, profissionais a partir de uma planilha padrão.
4. **Config.** Copiar `_template.yaml`, preencher, validar (`--validate`), aplicar.
5. **Conexão WhatsApp.** Número no WABA do cliente (ou número novo), `phone_number_id`, webhook apontado, templates utility submetidos para aprovação (leva de horas a dias — **iniciar aqui**).
6. **Conexão Google Calendar.** OAuth por profissional. Teste de leitura e escrita.
7. **Base de conhecimento.** Ingestão dos ativos + FAQ escrita. Revisão manual.
8. **Ensaio (obrigatório).** Rodar a suíte conversacional do tenant (seção 19.3) mais 10 conversas manuais cobrindo: preço, endereço, agendar, remarcar, cancelar, emergência, fora de escopo, pergunta sem resposta na base, cliente confuso, cliente hostil.
9. **Piloto assistido (3 a 7 dias).** Agente responde, atendente vê tudo no painel e corrige. Ajuste de config diário.
10. **Go-live.** Métricas ligadas, alerta de custo configurado, revisão semanal no primeiro mês.

**Checklist de aceite do cliente:** número respondendo, agenda espelhando corretamente, escalonamento chegando para a pessoa certa, lembretes saindo, painel acessível.

---
