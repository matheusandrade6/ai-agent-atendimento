# Suíte conversacional (seções 19.3 e 19.4)

Cada arquivo de `scenarios/` é uma conversa. O runner executa cada turno contra o agente
de verdade — motor, guardrails, tools e handoff — com o modelo, o calendário, o canal e a
tabela de handoffs trocados por dublês. Roda offline, sem Postgres, sem Redis e sem chave
de API.

```bash
pytest -m conversational          # a suíte inteira (é o que a 19.4 pede a cada mudança de prompt)
pytest tests/conversational -q
pytest tests/conversational -k emergencia -q
```

## Como escrever um cenário

Arquivo em `scenarios/<nome>.yaml`, com `scenario: <nome>` igual ao nome do arquivo (o
placar é indexado por ele). Depois, registre a linha em `placar.yaml`.

```yaml
scenario: exemplo
tenant: clinica-exemplo        # arquivo de config/tenants/
now: "2026-09-08T10:00:00"     # relógio congelado (opcional; padrão: terça, 08/09/2026, 10h)

services:                      # o que `list_services` devolve e o que entra no prompt
  - name: Consulta clínica
    price_line: "R$ 180,00"
knowledge:                     # o que `search_knowledge` pode encontrar
  - title: Espécies
    content: "Atendemos cães e gatos."
calendar_busy: ["+1 16:00"]    # slots ocupados na agenda falsa
tools:                         # tools que ainda não existem (S13/S15), declaradas aqui
  - name: check_availability
    slots: ["+1 14:30"]        # relativos ao `now`; o calendário falso resolve

turns:
  - user: "quanto custa a consulta?"
    agent:                     # o roteiro do modelo, ignorado em modo ao vivo
      - call: list_services
        arguments: { query: "consulta" }
      - say: "A consulta clínica custa R$ 180,00."
    expect:
      tools_called: [list_services]
      response_contains_any: ["180"]
```

O roteiro existe porque o placar precisa ser determinístico (ver o docstring de
`fakes.py`). Ele também permite o teste que a API real não permite: fazer o modelo errar
de propósito e provar que o erro não chega ao cliente — é o que fazem `preco_inventado` e
`horario_inventado`.

Chave desconhecida no YAML quebra o cenário. Assertion com nome errado não pode virar
assertion ignorada.

## Asserções

| Campo | O que confere |
|---|---|
| `handoff_opened` | Se um handoff foi aberto até este turno |
| `handoff_reason` | Motivo do handoff: id do gatilho, categoria da tool, com ou sem prefixo |
| `response_contains_any` | Pelo menos um dos trechos aparece na resposta |
| `response_excludes_any` | Nenhum dos trechos aparece |
| `no_tool_called` | Nenhuma dessas tools foi chamada (mesmo que a chamada tenha falhado) |
| `tools_called` | Todas essas foram chamadas |
| `max_turns_to_escalate` | A escalada aconteceu até o enésimo turno |
| `stage` | Estágio da conversa ao fim do turno (11.3) |
| `injection_flags` | Marcas que o detector de injeção registrou (D-30) |
| `prompt_isolates_user_content` | Invariante 7: a fala do cliente só aparece dentro de `<dado>` |
| `silenced` | RF-28: o agente ficou calado por handoff aberto |
| `tone` | Critério para o juiz LLM — **opcional, fora do placar** |

Texto é comparado sem acento e sem caixa.

## Placar (19.4)

`placar.yaml` registra, por cenário, o status e **quantas asserções ele faz**. O teste
`test_placar.py` bloqueia quatro coisas: cenário fora do placar, linha sem cenário, queda
de cenário aprovado e cenário enfraquecido (menos asserções do que o registrado).

`known_failure` é defeito conhecido, com motivo obrigatório, que **tem** de continuar
falhando — quando passar, a suíte manda promover a linha. Hoje há dois, ambos descritos
em `placar.yaml`.

## Juiz de tom e modo ao vivo

```bash
CONVERSATIONAL_JUDGE=1 pytest -m conversational   # avalia os critérios `tone` (rede, custa)
CONVERSATIONAL_LIVE=1  pytest -m conversational   # modelo de verdade no lugar do roteiro
```

Nenhum dos dois entra no placar: saída não determinística não bloqueia merge. Sem
`CONVERSATIONAL_JUDGE`, os critérios `tone` ficam versionados e não conferidos.

## O que falta

Dos 25 cenários obrigatórios da 19.3, estão escritos os que não dependem de agendamento.
Os demais (agendamento feliz, preferência impossível, remarcação, cancelamento dentro e
fora do prazo, slot tomado entre a oferta e a confirmação, duas conversas simultâneas,
áudio, cliente recorrente) entram com S13–S16, quando as tools existirem — o runner já
aceita o que eles vão precisar.
