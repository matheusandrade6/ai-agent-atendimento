# Defeitos conhecidos

Defeito encontrado e ainda não corrigido, um por entrada. Cada um tem um cenário da suíte
conversacional que o reproduz, registrado como `known_failure` em
`tests/conversational/placar.yaml` — enquanto o defeito existe, o cenário falha de
propósito; no dia em que for corrigido, a suíte manda promover a linha para `pass`
(`docs/DECISOES.md`, D-34).

Defeito corrigido sai daqui e vira teste verde. Isto não é changelog.

---

## DEF-01 · A mensagem de emergência sai com `{address}` e `{phone}` literais · achado na S11

**Cenário que reproduz.** `tests/conversational/scenarios/emergencia_placeholders.yaml`

**O que a pessoa recebe hoje.**

```
Isso pode ser uma emergencia. Vou chamar alguem da equipe agora.
Se estiver muito grave, va direto a clinica: {address}. Telefone: {phone}.
```

**Onde está.** `app/agent/guardrails.py:293` resolve os placeholders do `reply` do gatilho
com `_safe_format(trigger.reply, placeholders or {})`. Quem deveria passar esse
`placeholders` é o motor, em `app/agent/engine.py:346` — e ele chama
`guard.check(config=..., text=..., tenant_id=..., contact_id=..., signals=...)`, sem o
argumento. `_safe_format` é deliberadamente tolerante (`app/agent/guardrails.py:317`):
placeholder desconhecido fica literal em vez de levantar `KeyError`, porque um erro ali
deixaria a pessoa **sem resposta nenhuma** num gatilho de emergência. A tolerância está
certa; o que falta é a fonte dos valores.

**Por que não é só um `.format()` esquecido.** Não existe de onde tirar o endereço e o
telefone. A config de tenant não tem esses campos: `identity` guarda `name`, `vertical` e
`about`; o que existe com nome parecido é `persona.address_form` (forma de tratamento) e
`channels.whatsapp.phone_number_id` (id da conta do WhatsApp, não um telefone para
ligar). Corrigir exige **primeiro** decidir onde o endereço e o telefone da unidade moram.

**Impacto.** Alto, e no pior lugar possível: é a mensagem que manda a pessoa sair de casa
com um animal ferido. Ela precisa do endereço e do telefone justamente ali. Além do dano
prático, "{address}" na tela destrói a confiança no atendimento inteiro no momento em que
ela mais importa.

**O mesmo buraco vai aparecer na confirmação.** `messages.confirmation`
(`config/tenants/clinica-exemplo.yaml`) cita `{address}`, `{provider}`, `{price_line}`,
`{prep_line}`, `{date_human}`, `{time}`, `{service}` e `{subject_name}`. Ninguém formata
esse template hoje — quem vai fazer isso é a S15/S16. Se a fonte do endereço não existir
até lá, o defeito se repete na mensagem de confirmação de agendamento.

**Caminho de correção (esboço, não decisão).**

1. Campos de unidade na config de tenant — algo como `identity.address` e
   `identity.phone`, genéricos, com validação. Multi-unidade é tema da S12 (`resources`);
   se a decisão for esperar por ela, o gatilho de emergência não pode continuar citando
   um campo que não existe: ou o texto do YAML muda, ou o placeholder não resolvido é
   removido do texto antes de enviar.
2. `AgentEngine._input_guardrail` passa `placeholders` montado a partir desses campos.
3. Rede de segurança: placeholder que sobrar **não sai**. Uma varredura em
   `OutputGuardrail` (ou na saída do curto-circuito) que detecte `{\w+}` e descarte ou
   limpe o trecho. Sem isso, o mesmo erro volta no próximo template configurado.
4. Promover `emergencia_placeholders` para `pass` no placar.

---

## DEF-02 · Pedido de humano escala sem responder nada ao cliente · achado na S11

**Cenário que reproduz.** `tests/conversational/scenarios/pedido_humano_com_aviso.yaml`

**O que acontece hoje.** A pessoa escreve "quero falar com um atendente". O gatilho
`pedido_humano` casa por `match_intent: ask_for_human`, o handoff abre, a conversa é
silenciada por `handoff_silence_minutes` e o responsável é notificado por WhatsApp — tudo
certo do lado da equipe. Do lado da pessoa, **nada**: zero mensagem. E, por RF-28, o
agente também não responde às mensagens seguintes enquanto o silêncio durar. Quem pediu
para falar com uma pessoa fica olhando para uma conversa muda.

**Onde está.** `app/agent/guardrails.py:295`. O gatilho `pedido_humano` não declara
`reply`, e a mensagem genérica do tenant (`messages.out_of_scope`) só entra quando o
gatilho **não** escala:

```python
if not reply and not escalates:
    reply = config.messages.out_of_scope.strip()
```

Com `action: escalate`, `escalates` é `True`, a condição é falsa e `reply` continua vazio.
`split_message("")` devolve `()`, `TurnOutcome.messages` fica vazio e
`app/agent/runner.py` (em `_messages_of`) não enfileira envio nenhum — corretamente, já
que não há texto.

**Impacto.** Médio-alto. Não há dado errado nem promessa falsa; há abandono aparente
exatamente no pedido mais sensível depois da emergência. O efeito prático típico é a
pessoa repetir a mensagem várias vezes (que o agente, silenciado, ignora) e desistir.

**As duas correções possíveis, e a escolha não é óbvia.**

- **Config.** Um `reply` no gatilho `pedido_humano` de cada tenant ("Claro, já estou
  chamando alguém da equipe."). Resolve sem tocar em código e respeita a regra de ouro.
  Mas depende de cada onboarding lembrar — e o modo de falha é silencioso, que é o pior
  tipo de pendência de config.
- **Código.** Gatilho que escala sem `reply` usa um aviso padrão. Genérico, vale para todo
  tenant, e fecha o buraco de uma vez. Custo: mais um texto embutido no código, e é
  preciso decidir de onde ele vem — `messages.out_of_scope` diz "vou passar para alguém da
  equipe", que serve; usá-lo para escalada também deixaria a distinção entre recusa e
  escalada mais fraca.

Recomendação: **código**, com o texto vindo da config quando ela o tiver. A validação do
schema de tenant é o outro lugar onde isso podia ser resolvido (exigir `reply` em gatilho
que escala), e vale considerar junto.

**Ao corrigir.** Promover `pedido_humano_com_aviso` para `pass` no placar. Cuidado com o
cenário vizinho `pedido_humano`, que hoje afirma o comportamento correto do handoff
(motivo, turno da escalada, ausência de tools) e **não** afirma ausência de resposta — ele
deve continuar passando sem alteração.
