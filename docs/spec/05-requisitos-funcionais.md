## 5. Requisitos funcionais

### Atendimento e conversa

- **RF-01** Receber e responder mensagens de texto do WhatsApp em até 5s (p95) após o fim do buffer de agregação.
- **RF-02** Agregar mensagens fragmentadas do mesmo contato dentro de uma janela de debounce configurável (default 6s) antes de acionar o agente.
- **RF-03** Transcrever mensagens de áudio recebidas e tratá-las como texto.
- **RF-04** Receber imagens e documentos, armazená-los e sinalizar ao agente que existem (v1: não interpreta conteúdo clínico de imagem; anexa ao contexto do handoff).
- **RF-05** Responder dúvidas exclusivamente com base na base de conhecimento do tenant + config; nunca a partir de conhecimento geral sobre o negócio do cliente.
- **RF-06** Declarar desconhecimento e oferecer handoff quando a resposta não estiver na base.
- **RF-07** Manter histórico completo da conversa, com resumo rolante para conversas longas.
- **RF-08** Reconhecer contato recorrente e cumprimentar com contexto (nome, último agendamento).
- **RF-09** Responder no idioma/registro configurado pelo tenant (tom, tratamento, emojis sim/não, assinatura).

### Qualificação

- **RF-10** Coletar, antes de agendar, o conjunto de campos obrigatórios definidos na config do tenant (`intake_fields`), com validação por tipo.
- **RF-11** Suportar campos condicionais (ex.: "espécie" só para veterinária; "convênio" só se o tenant aceita convênio).
- **RF-12** Extrair campos implicitamente da conversa, sem reperguntar o que já foi dito.
- **RF-13** Identificar o serviço desejado a partir de linguagem natural, mapeando para o catálogo do tenant, incluindo sinônimos configurados.
- **RF-14** Detectar sinais de urgência/emergência configurados e disparar o protocolo de escalonamento imediato.

### Agendamento

- **RF-15** Calcular disponibilidade real cruzando: horário de funcionamento, jornada do profissional, free/busy do Google Calendar, duração do serviço, buffers, antecedência mínima, horizonte máximo e recursos.
- **RF-16** Perguntar a preferência de disponibilidade do cliente final (dias da semana, turno, janela de datas) e ordenar os slots propostos por aderência a ela.
- **RF-17** Propor no máximo N slots por vez (default 3), com opção de "mais horários".
- **RF-18** Criar hold com TTL (default 10 min) sobre o slot escolhido antes da confirmação.
- **RF-19** Confirmar o agendamento criando o evento no Google Calendar do profissional e persistindo o `appointment`.
- **RF-20** Garantir ausência de overbooking sob concorrência (seção 13.4).
- **RF-21** Enviar resumo de confirmação com serviço, profissional, data/hora, endereço, preço estimado e instruções de preparo.
- **RF-22** Permitir remarcar e cancelar por conversa, respeitando a política de antecedência do tenant.
- **RF-23** Refletir no sistema alterações feitas diretamente no Google Calendar (cancelamento/movimentação pelo prestador) e notificar o contato.
- **RF-24** Enviar lembrete configurável (default: 24h antes) e pedir confirmação de presença; registrar a resposta.
- **RF-25** Manter lista de espera e oferecer encaixe automático quando um horário vagar (Fase 3).

### Handoff e operação

- **RF-26** Escalar para humano por: pedido explícito, gatilho de risco, baixa confiança repetida, falha de ferramenta, ou tópico fora de escopo.
- **RF-27** Notificar o atendente responsável (WhatsApp e/ou painel) com resumo da conversa e motivo da escalada.
- **RF-28** Modo silencioso: enquanto houver handoff ativo, o agente não responde; retomada manual ou por timeout configurável.
- **RF-29** Painel com: caixa de conversas, takeover, lista de agendamentos, edição de base de conhecimento, horários e serviços.
- **RF-30** Registro de auditoria de toda ação com efeito externo (evento criado, mensagem enviada, handoff).

---
