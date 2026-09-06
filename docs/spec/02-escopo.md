## 2. Escopo

### 2.1 Dentro do escopo — v1

- Canais: **WhatsApp Cloud API** (principal) e **widget web** embutido no site do cliente.
- Agenda: **Google Calendar** por profissional/recurso, via OAuth do cliente.
- Agente com tool calling, base de conhecimento por tenant, guardrails.
- Agendar, remarcar, cancelar, consultar próximo agendamento.
- Lembretes automáticos e confirmação de presença.
- Handoff para atendente humano com notificação.
- Painel administrativo mínimo para o cliente (conversas, agendamentos, config básica, takeover).
- Multi-tenancy, observabilidade e custo por tenant.

### 2.2 Fora do escopo — v1 (registrado para não virar discussão)

- Canal de voz / telefonia.
- Instagram Direct e Messenger.
- Cobrança/pagamento dentro da conversa (sinal, pré-pagamento).
- Prontuário eletrônico, prescrição, anamnese estruturada.
- Integração com softwares verticais (iClinic, Simples Vet, Trinks, Belle) — previsto na arquitetura (seção 14.4) mas não implementado.
- App mobile próprio.
- Marketing ativo / campanhas de reativação em massa.

### 2.3 Não-objetivos permanentes

- O agente **não dá diagnóstico, orientação clínica, prescrição ou conselho de saúde**. Em qualquer sinal disso, responde com escopo e escala.
- O agente **não negocia preço** fora da tabela configurada.
- O agente **não inventa disponibilidade** — só oferece slot que veio do motor de disponibilidade.

---
