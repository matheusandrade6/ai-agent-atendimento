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

## D-14 · O job de entrada carrega a mensagem, não só os ids · S05

**Contexto.** A S04 (D-13) entregou `enqueue_inbound(tenant_id, conversation_id)`. Para
agregar a rajada, o worker precisa saber **quais mensagens ainda não viraram turno**.

**Problema.** Com só os ids, o worker teria que reler `messages` do banco e descobrir o
que está pendente — o que exige uma coluna de "já processada" (migration nova, escrita a
mais no caminho quente) ou uma heurística por timestamp, que erra em reentrega.

**Decisão.** O job leva a mensagem inteira (`InboundMessage`: ids, canal, tipo, texto,
`media_ref`, `provider_msg_id`). Quem sabe o que ainda não virou turno é o buffer em
Redis, não o banco. O payload trafega como `dict` JSON, não como objeto: produtor e
consumidor são processos separados que convivem em versões diferentes durante um deploy.

**Consequência.** Os testes da S04 foram atualizados (regra do SESSIONS.md), não
contornados. O banco continua sendo a fonte de verdade do histórico; o Redis é a fonte
de verdade do *que está em voo*, e perder o Redis atrasa turnos sem perder mensagem.

---

## D-15 · Debounce por sequência monotônica, não por timer cancelável · S05

**Contexto.** A 14.1.2 pede que cada mensagem reinicie o timer de `debounce_seconds`.

**Problema.** "Reiniciar o timer" sugere cancelar o disparo agendado. Cancelamento em
fila distribuída é corrida pura: entre `cancel` e `schedule` cabe o disparo antigo
(dois turnos para a mesma rajada) e entre `drain` e `append` cabe a mensagem nova
(rajada perdida).

**Decisão.** Nada é cancelado. Cada mensagem incrementa `agg:{id}:seq` e agenda um
disparo que **declara qual sequência esperava ver**. O disparo só fecha a rajada se o
contador ainda for aquele; senão, desiste em favor do disparo mais novo. Ler-e-apagar o
buffer é um script Lua, então não existe janela entre conferir e consumir.

**Prova das duas garantias.** Um turno por rajada: o *drain* é atômico, só um chamador
leva as partes; um disparo repetido encontra a lista vazia. Nenhuma mensagem perdida: ou
ela entra antes do disparo (e o contador avança, empurrando a rajada para o disparo dela)
ou entra depois (e abre a próxima rajada). O teste
`test_mensagem_que_chega_junto_do_disparo_nao_se_perde` roda os dois interleavings com
`asyncio.gather`, em Redis real, e aceita os dois desfechos — o que ele proíbe é
duplicata, sumiço ou turno vazio.

**Idempotência.** `agg:{id}:seen` guarda os ids já bufferizados: um job reentregue pelo
arq não duplica a parte, mas **reagenda** o disparo — sem isso, a última mensagem da
rajada poderia ficar presa no buffer até a pessoa escrever de novo.

---

## D-16 · Token do WhatsApp é global; o que é por tenant é o `phone_number_id` · S05

**Contexto.** O envio precisa de um token de acesso, e a spec só define secret store por
tenant para o Google (`providers.credentials_ref`, 14.3).

**Decisão.** `WHATSAPP_ACCESS_TOKEN` é uma setting global — um token de sistema da
Business Manager cobre os WABAs sob ela. O que separa um tenant do outro no envio é o
`phone_number_id`, que vem da config do tenant. Nenhum `if tenant == 'x'`: é o mesmo
código com um número diferente na URL.

**Consequência e limite.** Cliente com WABA em Business Manager própria não cabe nesse
modelo. Quando aparecer, o token vira `channels.whatsapp.credentials_ref` no mesmo
padrão do Google, sem tocar no worker. Sem token configurado, o canal simplesmente não
existe e o worker segue rodando — que é o estado normal em desenvolvimento.

---

## D-17 · Fora da janela de 24h, texto livre é recusado, não adiado · S05

**Contexto.** A 14.1.4 proíbe texto livre fora da janela de serviço; o envio proativo
exige template utility aprovado, que é entrega da S19.

**Decisão.** O worker de saída checa `service_window_expires_at` antes de chamar o canal.
Fechada, a mensagem **não é enviada**, vira linha `blocked` em `messages` e sai log de
erro. Não há retentativa: o tempo não reabre a janela.

**Por que não guardar para mandar depois.** Uma resposta de agendamento que chega horas
atrasada é pior que nenhuma. E persistir a tentativa como `blocked` é o que faz o
atendente ver, na caixa de conversas (15.2), que houve silêncio e por quê — em vez de um
buraco inexplicado.

**Retentativa do que é transitório.** `429` reagenda honrando `Retry-After`; `5xx` e
timeout reagendam com backoff exponencial de *equal jitter*; `4xx` marca `failed` e para.
Esgotadas as tentativas, a mensagem também vira linha `failed`: fila de saída não
descarta em silêncio.

---

## D-18 · Settings de worker por decorador, porque o arq lê o `__dict__` da classe · S05

**Contexto.** Quatro perfis de worker (tudo-em-um, entrada, saída, cron) compartilham
`redis_settings`, `on_startup`, limites.

**Problema encontrado na verificação.** Herança **não funciona** aqui: o arq monta o
worker a partir de `settings_cls.__dict__`, então uma subclasse que só troca `functions`
perde `redis_settings` sem um único erro — e o worker sobe apontando para o Redis default
`localhost:6379`. A falha só aparece em produção, como fila que não anda.

**Decisão.** Um decorador (`_com_padroes`) grava os atributos comuns no `__dict__` de
cada classe. `tests/unit/test_worker_settings.py` afirma, para os quatro perfis, que o
worker resolve o Redis da aplicação, os limites e o ciclo de vida.

**Nota sobre o cron.** O arq recusa subir um worker sem nenhuma função nem cron
registrado. Como a S05 entrega o cron *vazio* (os jobs são da S19 e da S20), ele carrega
um único job — `heartbeat` —, que também serve de sinal barato de "o cron está vivo" para
o alerta da S22.

---

## D-19 · Relógio da VM do Docker no Windows atrapalha teste com TTL curto · S05

**Sintoma.** Testes de agregação falhavam de forma intermitente, com o buffer sumindo do
Redis no meio da rajada — a cara de uma corrida.

**Causa.** Não era corrida. O relógio do Redis dentro da VM do Docker Desktop ficava
~72s atrás do host e corrigia de uma vez; o salto para frente expira instantaneamente
qualquer chave com TTL menor que o salto. Medido com `TIME` amostrado a cada 10ms.

**Decisão.** Os testes usam TTL folgado (1h) no agregador. O default de produção
(`aggregation_ttl_seconds = 900`) fica como está — em host com relógio sadio, 15 minutos
é folga suficiente sobre o maior `debounce_seconds` configurável (60s).

**Para quem for depurar isso de novo:** antes de suspeitar de concorrência em teste com
TTL, compare `redis TIME` com o relógio do host.

---

## D-20 · Pensamento adaptativo com esforço baixo, e não pensamento desligado · S06

**Contexto.** O agente decide *o que perguntar* e *quando chamar tool* — decisão que se
beneficia de raciocínio. Mas roda sob `limits.max_llm_cost_usd_per_conversation`, cujo
default é **US$ 0,15 para até 60 mensagens**: cerca de US$ 0,007 por turno no Sonnet 5.

**Decisão.** `thinking: {"type": "adaptive"}` com `output_config.effort = "low"`, ambos
em settings (`LLM_THINKING`, `LLM_EFFORT`) para subir por ambiente sem tocar no código.

**Por que não desligar.** Com o pensamento desligado o modelo escreve chamada de tool no
texto visível em vez de emitir o bloco `tool_use` — o turno "dá certo", a tool nunca roda
e ninguém vê erro. Num loop de tool calling esse texto ainda contamina os turnos
seguintes. Esforço baixo custa menos do que essa classe de bug.

---

## D-21 · O prompt sai em dois segmentos, com o corte de cache depois de `[ESTILO]` · S06

**Contexto.** O cache de prompt é casamento de **prefixo**: um byte diferente invalida
tudo dali para a frente. A ordem dos blocos da 11.2 mistura o que é estável (identidade,
papel, limites, estilo) com o que muda a cada turno (intake, RAG, "agora").

**Decisão.** `build_system_prompt` devolve dois `PromptSegment`: o estável, com
`cache_breakpoint=True`, e o volátil. A ordem dos blocos da spec é preservada
integralmente — o corte cai exatamente na fronteira entre os dois grupos.

**Consequência.** Prefixo curto demais simplesmente não é cacheado pela API: não há erro,
só não há desconto. `tests/unit/test_agent_prompt.py` prova que o segmento estável não
contém nada que varie entre turnos — sem isso o cache nunca teria acerto e o desconto
sumiria em silêncio.

---

## D-22 · Condição de intake tem interpretador próprio, não `eval` · S06

**Contexto.** O YAML do tenant traz `when: "service.name contains 'vacina'"`. Alguém
precisa avaliar isso, e quem avalia decide se um campo passa a ser obrigatório.

**Decisão.** `app/domain/conditions.py` — gramática fechada de **uma** comparação:
`caminho operador literal`. Nada mais parseia. `ConditionalIntake` valida a expressão no
carregamento da config, então `when` torto derruba o onboarding e não a primeira conversa.

**Por que não `eval`.** Arquivo de config é editado no onboarding de cliente, não é código
de aplicação. `eval` transformaria um erro de digitação em execução arbitrária dentro do
worker.

**Detalhe que custou um teste.** O literal é ancorado no fim da expressão. Sem isso,
`x == 'a' and y == 'b'` seria aceito como comparação contra o texto `'a' and y == 'b'` —
não executaria nada, mas passaria despercebido até a pergunta condicional aparecer na
hora errada.

---

## D-23 · `messages.created_at` usa `clock_timestamp()`, não `now()` · S06

**Sintoma.** Teste de carga de histórico devolvendo a resposta do agente **antes** da
pergunta do cliente.

**Causa.** No Postgres, `now()` é o instante do **início da transação**. Duas mensagens
gravadas na mesma transação — uma rajada que chega num único payload da Meta — recebem
`created_at` idêntico. Como a PK é `gen_random_uuid()`, não existe critério de desempate:
`ORDER BY created_at, id` ordena por um número aleatório.

**Decisão.** Migration 0005 troca o default de `messages.created_at` para
`clock_timestamp()`, que é o instante da própria linha. A janela de contexto do agente
depende dessa ordem — histórico embaralhado é alucinação garantida, com o modelo
"respondendo" antes de ser perguntado.

---

## D-24 · `dispose_engine` limpa o cache antes de fechar e engole a falha · S06

**Sintoma.** Testes de integração falhando com `Event loop is closed` — sempre no teste
*seguinte* ao que vazou a conexão.

**Causa.** O engine é singleton de módulo e o pool guarda conexões amarradas ao event
loop em que foram abertas. `dispose_engine` limpava as globais **depois** do
`await engine.dispose()`; quando o dispose levantava, o engine quebrado continuava
cacheado e envenenava toda conexão seguinte.

**Decisão.** Limpar as globais primeiro e registrar a falha do `dispose()` em vez de
propagá-la. Um pool que não conseguiu se despedir é um problema menor do que um engine
quebrado que continua sendo servido.

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

Estado ao fim da S06, contra Postgres 16 real:

| Item | Estado |
|---|---|
| Migration `0005` (`upgrade` e `downgrade`) | ✅ aplicada e revertida |
| Motor do agente, memória, prompt e condições | ✅ 132 testes novos |
| Total: 230 testes | ✅ verde |
| `ruff check` + `ruff format --check` | ✅ limpo |
| `mypy --strict` | ✅ limpo (42 arquivos) |
