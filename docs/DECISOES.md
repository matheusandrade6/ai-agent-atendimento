# Decisões de implementação

Registro de escolhas tomadas durante a implementação que detalham ou divergem da spec.
Uma entrada por decisão, com a sessão que a tomou.

---

## D-01 · `FORCE ROW LEVEL SECURITY` é obrigatório · S02

**Contexto.** A spec pede RLS com `USING (tenant_id = current_setting('app.tenant_id'))`.

**Problema.** No Postgres, o **dono da tabela ignora a policy** por padrão. Como aqui o dono
é o mesmo usuário que a aplicação usa, a RLS ficaria decorativa: o isolamento passaria nos
testes escritos de forma ingênua e vazaria em produção.

**Decisão.** Toda tabela recebe `ENABLE` **e** `FORCE ROW LEVEL SECURITY`. O teste
`test_todas_as_tabelas_com_tenant_id_tem_rls_forcada` lê `pg_class.relforcerowsecurity` e
falha se alguma tabela ficar de fora.

---

## D-02 · A policy é fail-closed · S02

**Decisão.** A policy usa `current_setting('app.tenant_id', true)` — com o segundo argumento
em `true`, a função devolve NULL em vez de levantar erro quando a variável não foi definida.
Como comparação com NULL é falsa, **o padrão passa a ser não ver nada**.

**Consequência.** Esquecer de abrir o contexto de tenant produz "zero linhas", nunca
"linhas de outro cliente". O modo de falha é visível e inofensivo.

---

## D-03 · Válvula de escape explícita: `app.bypass_rls` · S02

**Problema.** Com FORCE ligado, migrations, onboarding, o roteamento de webhook (que precisa
descobrir o tenant *antes* de tê-lo) e jobs de cron que varrem a base inteira ficariam sem
acesso.

**Alternativa descartada.** Um segundo papel de banco com `BYPASSRLS`. Correto, porém obriga
a gerenciar dois usuários, dois segredos e dois pools desde o dia 1.

**Decisão.** Uma segunda condição na policy: `current_setting('app.bypass_rls') = 'on'`,
acessível apenas pelo context manager `bypass_rls_session()`. O nome é deliberadamente
constrangedor para saltar aos olhos em code review.

**Revisitar quando.** Ao separar o usuário de aplicação do dono das tabelas — aí o papel
dedicado com `BYPASSRLS` passa a ser a opção melhor.

---

## D-04 · Segredo do tenant não é resolvido antes de gravar · S03

**Contexto.** A spec (10.1) diz que o YAML é "materializado em `tenants.config` (JSONB)" e
(18.1) que segredo nunca vai para o YAML, que usa `${VAR}`.

**Problema.** Resolver `${VAR}` no momento de gravar colocaria o segredo dentro do banco —
exatamente o que a seção 18.1 evita no arquivo.

**Decisão.** `tenants.config` guarda o **valor bruto, com os `${VAR}` preservados**. A
resolução acontece na leitura, em `TenantConfig.resolved()`. Segredo vive só no ambiente.

---

## D-05 · `tzdata` é dependência de produção · S01

**Problema.** `zoneinfo` depende da base IANA do sistema operacional. Ela não existe no
Windows e não vem nas imagens `python:*-slim`. A falha aparece como
`ZoneInfoNotFoundError: America/Sao_Paulo` — ou seja, **RNF-06 quebra em silêncio** até
alguém tentar calcular um horário.

**Decisão.** `tzdata` entra em `dependencies`, não em `dev`.

---

## D-06 · `service_providers` e `service_resources` não têm `tenant_id` · S02

Seguem a DDL da spec. São tabelas de junção alcançáveis apenas por `services`, `providers` e
`resources`, todas com RLS. Um join a partir delas não expõe linha de outro tenant porque as
pontas estão filtradas.

**Risco residual.** Uma escrita malformada poderia ligar um serviço do tenant A a um
profissional do tenant B. Nada no banco impede isso hoje.

**Mitigação prevista.** A criação desses vínculos passa por `scripts/onboard_tenant.py`
(S23) e pelo painel (S18), que validam que as duas pontas pertencem ao mesmo tenant. Se o
vínculo passar a ser criado em mais lugares, promover para constraint composta.

---

## D-07 · `tenants` não tem RLS · S02

A tabela não tem coluna `tenant_id` — ela *é* a lista de tenants. O roteamento do webhook
precisa lê-la antes de saber qual é o tenant. Fica protegida pela aplicação, não pelo banco.
Com D-04, ela não guarda segredo.

---

## D-08 · Colunas acrescentadas à DDL da spec · S02

Campos que a spec exige em texto mas não lista na DDL da seção 9:

| Tabela | Coluna | Motivo |
|---|---|---|
| `providers` | `watch_channel_id`, `watch_expires_at` | Renovação do canal `events.watch` (13.5), necessária em S20. |
| `conversations` | `silenced_until` | Modo silencioso do handoff com retomada por timeout (RF-28). |
| `messages` | `billing_category` | Métrica de custo precisa separar mensagem gratuita de paga (14.1.4). |
| `appointments` | `sync_status` | Estado `pending_sync` quando o Google falha de forma persistente (14.3). |
| `knowledge_chunks` | `ordinal` | Ordem do chunk no documento; reingestão idempotente (S07). |
| `handoffs` | `summary`, `notified_at` | RF-27 pede resumo na notificação; `notified_at` evita notificar duas vezes. |
| `reminders` | `sent_at` | Distinguir "agendado" de "enviado" na métrica de no-show. |
| — | `admin_users` | Tabela nova: a seção 15 exige papéis `owner`/`attendant`/`support`. |

---

## Pendências de verificação

| Item | Estado | Como fechar |
|---|---|---|
| Migrations `0001`–`0003` aplicadas | **não executado** | Docker Desktop não sobe nesta máquina (serviço `com.docker.service` parado, exige elevação). Rodar `make up && make migrate`. |
| `tests/integration/test_rls.py` | **não executado** | Depende do item acima. Sem Postgres, os testes são pulados, não falham. |
| `mypy --strict` | **não executado** | Instalação do `mypy` interrompida por falha de rede no PyPI. Rodar `make lint`. |
| Testes unitários (31) | ✅ verde | — |
| `ruff check` + `ruff format` | ✅ limpo | — |
