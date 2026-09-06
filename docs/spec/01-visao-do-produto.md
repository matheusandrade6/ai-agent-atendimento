## 1. Visão do produto

### 1.1 Problema

Prestadores de serviço com atendimento por hora marcada (clínicas médicas e veterinárias, nutricionistas, fisioterapeutas, massoterapeutas, salões, estúdios) perdem receita em três pontos:

- **Latência de resposta.** O contato chega por WhatsApp fora do horário ou durante o atendimento; a resposta demora horas e o lead procura outro.
- **Custo de recepção.** Uma recepcionista gasta a maior parte do tempo respondendo as mesmas 15 perguntas (preço, convênio, endereço, estacionamento, o que levar) e negociando horário.
- **Agenda mal preenchida.** Cancelamentos de última hora deixam buracos que ninguém preenche ativamente.

### 1.2 Proposta

Um agente conversacional que atua como primeira linha de atendimento no WhatsApp (e em widget no site), capaz de:

- responder dúvidas com base na base de conhecimento **daquele cliente**;
- **qualificar** o contato — entender quem é, o que precisa, se é primeira vez, qual serviço/profissional, convênio/forma de pagamento, urgência;
- **propor horários reais** cruzando a agenda do prestador com a disponibilidade declarada pelo cliente final;
- **efetivar o agendamento** criando o evento na agenda do prestador e confirmando pelos dois lados;
- **remarcar e cancelar**;
- **lembrar e confirmar** presença antes do horário;
- **escalar para humano** quando sai do escopo, quando há risco, ou quando o cliente final pede.

### 1.3 Modelo de negócio (contexto para decisões técnicas)

- Produto padronizado, **setup personalizado por cliente** (linguagem, serviços, regras, base de conhecimento, integrações).
- Cobrança recorrente por tenant + setup inicial.
- Datamind opera a infraestrutura. O cliente não hospeda nada.
- **Consequência técnica:** multi-tenancy real desde o dia 1, custo por conversa observável por tenant, e onboarding de novo cliente deve ser um runbook de horas — não de semanas (seção 16).

### 1.4 Verticais alvo (v1)

| Vertical | Particularidade que a config precisa cobrir |
|---|---|
| Clínica veterinária | Paciente ≠ contato (o pet). Espécie/porte muda duração e preço. Urgência/emergência exige escalonamento imediato. |
| Clínica médica / odontológica | Convênios, primeira consulta vs retorno, preparo prévio, dados de saúde (LGPD reforçada). |
| Nutricionista / fisioterapeuta / psicólogo | Sessões recorrentes, pacotes, online vs presencial. |
| Salão de beleza / estética | Combo de serviços na mesma visita (duração somada), profissional preferido, tempo de máquina/cadeira. |
| Massoterapia / terapias | Duração variável, sala como recurso escasso. |

Essas diferenças são **dados de configuração**, não código.

---
