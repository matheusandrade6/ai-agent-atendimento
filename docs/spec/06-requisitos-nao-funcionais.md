## 6. Requisitos não-funcionais

- **RNF-01 Latência.** p95 de resposta ≤ 5s; p99 ≤ 12s. Respostas longas usam mensagem de "digitando" nativa.
- **RNF-02 Disponibilidade.** 99,5% mensal para o webhook de ingestão. Ingestão nunca depende do LLM: recebe, persiste, responde 200, processa assíncrono.
- **RNF-03 Isolamento.** Nenhum dado de um tenant pode aparecer em outro. Todas as queries filtram por `tenant_id`; RLS habilitada no Postgres.
- **RNF-04 Idempotência.** Todo webhook e toda escrita externa são idempotentes por chave natural.
- **RNF-05 Custo.** Custo de LLM por conversa rastreado por tenant; alerta ao ultrapassar teto configurado.
- **RNF-06 Timezone.** Todo timestamp armazenado em UTC (`timestamptz`); toda apresentação e regra de negócio na timezone da unidade. Nenhuma aritmética de data ingênua sobre horário local.
- **RNF-07 LGPD.** Base legal, minimização, retenção e direito de exclusão implementados (seção 18).
- **RNF-08 Observabilidade.** Tracing distribuído por conversa; toda chamada de LLM e tool logada com input/output/tokens/latência.
- **RNF-09 Portabilidade de modelo.** A camada de LLM é abstraída; trocar de modelo/provedor não deve exigir mudança em regras de negócio.
- **RNF-10 Setup.** Onboarding de um novo tenant executável em ≤ 4 horas de trabalho por um operador, sem deploy.

---
