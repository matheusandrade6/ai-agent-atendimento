## 3. Glossário do domínio

| Termo | Definição |
|---|---|
| **Tenant** | Cliente da Datamind (uma clínica, um salão). Unidade de isolamento de dados e configuração. |
| **Unidade** (`location`) | Endereço físico de um tenant. Um tenant pode ter várias. Define timezone e horário de funcionamento. |
| **Profissional** (`provider`) | Quem executa o atendimento. Possui uma agenda Google conectada. |
| **Recurso** (`resource`) | Ativo escasso não-humano necessário ao serviço (sala, cadeira, equipamento). Opcional por tenant. |
| **Serviço** (`service`) | Item agendável: "Consulta clínica geral", "Corte + escova", "Vacina V10". Tem duração, buffers, preço, requisitos. |
| **Slot** | Intervalo candidato calculado pelo motor de disponibilidade. Não existe em banco até virar hold. |
| **Hold** | Reserva temporária de um slot durante a conversa (TTL curto), para evitar corrida. |
| **Agendamento** (`appointment`) | Compromisso confirmado, espelhado como evento no Google Calendar. |
| **Contato** (`contact`) | Pessoa do outro lado da conversa (identificada por telefone ou sessão web). |
| **Paciente/Cliente-alvo** (`subject`) | Quem recebe o atendimento. Pode ser o próprio contato ou um dependente/pet. |
| **Conversa** (`conversation`) | Thread contínua com um contato em um canal. |
| **Janela de serviço** | Janela de 24h aberta quando o contato envia mensagem, dentro da qual a resposta é livre e gratuita no WhatsApp. |
| **Handoff** | Transferência da conversa para atendente humano; agente entra em modo silencioso. |

---
