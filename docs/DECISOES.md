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

**Alternativa descartada.** Um segundo papel de banco com `BYPASSRLS`. Dá isolamento no
nível do papel, mas exige um terceiro conjunto de credenciais e um pool próprio só para
tarefas administrativas.

**Decisão.** Uma segunda condição na policy: `current_setting('app.bypass_rls') = 'on'`,
acessível apenas pelo context manager `bypass_rls_session()`. O nome é deliberadamente
constrangedor para saltar aos olhos em code review.

**Nota (atualizada por D-09).** A separação entre papel de aplicação e dono das tabelas
já existe — mas por outro motivo, e ela **não substitui esta válvula**: `datamind_app` é
`NOBYPASSRLS` de propósito, então continua precisando da GUC para o roteamento de webhook
e para os jobs que varrem tenants. Um terceiro papel com `BYPASSRLS` só se justifica se
esses caminhos crescerem além de um punhado.

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

## D-09 · A aplicação conecta com um papel sem privilégio · S02

**Como apareceu.** Com as migrations aplicadas, `test_tenant_a_nao_ve_linha_de_b` falhou:
o tenant A enxergou as duas linhas. A RLS estava ligada, forçada e com a policy correta.

**Causa.** O usuário que a imagem do Postgres cria a partir de `POSTGRES_USER` é
**SUPERUSER**, e superusuário **ignora RLS por completo**. `FORCE ROW LEVEL SECURITY`
resolve o caso do *dono* da tabela (D-01), mas não alcança superusuário — são duas
formas diferentes de contornar a policy, e eu só tinha coberto uma.

**Decisão.** Dois papéis:

| Papel | Privilégio | Uso |
|---|---|---|
| `datamind` | SUPERUSER, dono das tabelas | Só migrations. Nunca serve request. |
| `datamind_app` | NOSUPERUSER, NOBYPASSRLS | Aplicação, workers e testes. |

`Settings.database_url` aponta para `datamind_app`; `database_admin_url`, usado apenas
pelo Alembic, aponta para o dono. O papel nasce em
`docker/postgres/init/01-app-role.sql` e recebe os GRANTs na migration `0004`.

**Guarda permanente.** `test_aplicacao_nao_conecta_como_superusuario` lê
`pg_roles.rolsuper` e `rolbypassrls` do `current_user` e falha se a aplicação voltar a
conectar privilegiada. Ele roda **antes** dos demais testes de isolamento, porque sem ele
todos os outros passariam a testar nada. Verifiquei que ele falha de fato: apontando
`DATABASE_URL` de volta para `datamind`, quatro testes de isolamento caem junto com ele.

**Lição que vale para as próximas sessões.** RLS tem três formas de ser contornada —
superusuário, `BYPASSRLS` e dono sem `FORCE`. Fechar duas não protege.

---

## D-10 · `opentelemetry-exporter-otlp-proto-http` faltava nas dependências · S01

`setup_tracing` importa o exporter OTLP, mas só `opentelemetry-api`, `-sdk` e
`-instrumentation-fastapi` estavam declarados. Com `OTEL_ENABLED=true` a aplicação
quebraria no startup. Detectado pelo `mypy --strict` (`import-not-found`).

---

## D-11 · `tasks.ps1` para Windows · S01

`make` não existe numa instalação padrão do Windows e o PowerShell 5.1 não aceita `&&`
para encadear comandos — o Makefile é inútil nesta máquina. `tasks.ps1` expõe as mesmas
tarefas (`up`, `install`, `migrate`, `test`, `lint`, `check`) e verifica o engine do
Docker antes de tentar subir os containers, com a instrução de recuperação no erro.

---

## D-12 · Roteamento do webhook varre os tenants ativos, sem cache · S04

**Contexto.** O webhook recebe `phone_number_id` e precisa achar o tenant dono do
numero (14.1.1) antes de ter um `tenant_id` para abrir RLS. `tenants.config` guarda o
YAML bruto, com `${VAR}` preservado (D-04) — não dá para comparar o valor resolvido
direto numa cláusula `WHERE` em JSONB.

**Decisão.** `resolve_tenant_by_phone_number_id` lê todos os tenants com
`status = 'active'` via `bypass_rls_session()` (a válvula de escape de D-03, no uso
que o próprio `app.core.db` já documenta: "roteamento de webhook, que precisa
descobrir o tenant antes de ter um"), resolve a config de cada um com
`TenantConfig.resolved()` e compara `channels.whatsapp.phone_number_id`. Sem cache.

**Por que não cachear agora.** Volume de tenants em v1 é dezenas, não milhares — uma
varredura completa por request cabe folgado no orçamento de <1s do webhook. Cache
introduziria invalidação (config muda por deploy, não por commit) sem necessidade
comprovada. Revisitar se o número de tenants ativos crescer o bastante para pesar.

**Config de tenant inválida não derruba o roteamento.** Uma linha que falhe
`TenantConfig.model_validate` é ignorada com log de aviso, não propaga exceção — um
tenant mal configurado não pode impedir os outros de receber mensagem.

---

## D-13 · Contrato de handoff para a fila (S04 -> S05) · S04

**Contexto.** SESSIONS.md pede que S04 já enfileire, mas o worker que consome a fila
com debounce (14.1.2) é da S05. Sem o consumidor, o contrato do produtor precisa ser
estável o bastante para não travar S05, mas simples o bastante para não antecipar
decisão que não é desta sessão.

**Decisão.** `app.workers.queue.InboundQueue` é um `Protocol` com
`enqueue_inbound(tenant_id, conversation_id)`. `ArqInboundQueue` é a implementação
real (arq), guardada em `app.state.inbound_queue` e trocável por um fake nos testes
sem `Depends` — o webhook lê direto de `request.app.state`. O nome do job
(`INBOUND_JOB_NAME = "process_inbound"`) e o payload são deliberadamente mínimos:
S05 é livre para mudar os dois, desde que atualize os testes desta sessão (regra do
SESSIONS.md) em vez de contornar.

**Falha ao enfileirar nunca derruba o webhook.** A mensagem já está persistida antes
do `enqueue_inbound`; se o Redis estiver fora, a conexão tenta uma única vez (sem o
backoff de 5 tentativas do arq, que sozinho estouraria o orçamento de <1s) e loga
erro. O turno fica atrasado, não perdido — reprocessamento de mensagens órfãs é
trabalho futuro, fora do escopo desta sessão.

---

## Pendências de verificação

Todas fechadas. Estado verificado ao fim da Fase 0, contra Postgres 16 real:

| Item | Estado |
|---|---|
| Migrations `0001`–`0004` | ✅ aplicadas |
| Isolamento entre tenants (6 testes) | ✅ verde |
| Rede anti-overbooking (4 testes) | ✅ verde |
| Testes unitários (32) | ✅ verde |
| Total: 42 testes | ✅ verde |
| `ruff check` + `ruff format --check` | ✅ limpo |
| `mypy --strict` | ✅ limpo (22 arquivos) |
