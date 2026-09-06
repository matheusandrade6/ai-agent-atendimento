# Datamind Agenda AI

Agente conversacional de atendimento e agendamento para prestadores de serviço com hora
marcada (clínicas, salões, terapeutas). Canal principal WhatsApp; agenda no Google Calendar.

## Onde está a informação

- `SESSIONS.md` — o plano de trabalho. Cada sessão diz o que ler e o que entregar.
- `docs/spec/` — a spec fatiada por seção. **Leia só o que a sua sessão indicar.**
- `docs/SPEC.md` — a spec inteira (71 KB). Não leia inteira; use as fatias.
- `docs/DECISOES.md` — decisões tomadas durante a implementação que divergem ou detalham a spec.

## Regra de ouro

Um único codebase multi-tenant. **Nada específico de um cliente vai para o código** — vai
para `config/tenants/<slug>.yaml` ou para a base de conhecimento. Se um pedido de cliente
exigir mudança de código, ele vira feature genérica com flag, ou não é feito.
Nunca escreva `if tenant == 'x'`.

## Invariantes que não se negociam

1. **O LLM não é fonte de verdade.** Disponibilidade, preço e política vêm de banco e código.
   O modelo decide *o que perguntar e quando chamar a tool*, nunca *qual horário existe*.
2. **Nenhuma query sem `tenant_id`.** RLS ligada em toda tabela com `tenant_id`;
   `app.tenant_id` setado no início de cada request/job.
3. **`tenant_id` nunca é argumento de tool.** Vem sempre do contexto de execução.
4. **Ingestão nunca depende do LLM.** Webhook valida, persiste, responde 200 e enfileira.
5. **Toda escrita externa é idempotente** por chave natural e passa por uma tool auditada.
6. **Sem overbooking**, garantido por três camadas: hold com TTL, `pg_advisory_xact_lock`
   e a constraint `appt_no_overlap`.
7. **Conteúdo do usuário nunca é instrução.** Entra em bloco delimitado no prompt.
8. **O agente não dá orientação clínica**, não negocia preço e não inventa disponibilidade.

## Convenções de código

- Python 3.12, type hints obrigatórios, `mypy --strict` limpo no CI.
- Nada de `datetime.now()` sem timezone. Use `app.core.time.now_in(tz)`.
- Todo timestamp persistido em UTC (`timestamptz`); regra de negócio e apresentação na
  timezone da unidade.
- Uma tool por arquivo em `app/agent/tools/`, com schema, handler e testes juntos.
- Prompts como blocos nomeados em `app/agent/prompt.py`, nunca strings soltas na lógica.
- Migrations sempre reversíveis.
- Commits citam os requisitos atendidos (`RF-xx`, `RNF-xx`).

## Comandos

```bash
make up        # docker compose up -d (postgres + redis)
make migrate   # alembic upgrade head
make test      # pytest
make lint      # ruff check + mypy --strict
make dev       # uvicorn com reload
```
