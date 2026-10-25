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

## D-25 · Reingestão substitui os chunks por completo, não faz upsert · S07

**Contexto.** A 11.6 pede que reingerir um documento não duplique chunk. O número de
chunks de um documento muda sempre que o conteúdo muda de tamanho — não há um `ordinal`
estável para um upsert calcular contra.

**Decisão.** `ingest_document` apaga, na mesma transação, todos os chunks do
`document_id` e insere os novos apurados do conteúdo atual. Reingerir o mesmo conteúdo
produz o mesmo conjunto de chunks; reingerir conteúdo editado produz exatamente o
conjunto novo, sem sobra do anterior.

**Consequência.** Mais I/O do que um upsert (apaga tudo, reinsere tudo) em troca de não
precisar de lógica de diff. Para o volume de uma base de conhecimento por tenant (FAQ,
políticas, instruções de preparo), o custo é irrelevante.

---

## D-26 · `EmbeddingProvider` ainda sem implementação real · S07

**Contexto.** A 11.6 descreve o pipeline de RAG — chunk, embedding, top-k por cosseno —
mas não escolhe fornecedor de embeddings, e o projeto não tem SDK de nenhum (a Anthropic
não expõe endpoint de embeddings). `knowledge_chunks.embedding` já é `VECTOR(1536)`
desde a S02.

**Decisão.** `app/knowledge/embeddings.py` define a interface `EmbeddingProvider` e uma
implementação local, `HashingEmbeddingProvider` — bag-of-words com hash, determinística,
sem rede. Ela sustenta os testes de ingestão/recuperação (dedup, isolamento de tenant,
corte de score) rodando offline no CI, mas não tem qualidade semântica de embedding
real.

**Consequência.** Antes de produção, alguém precisa escolher um fornecedor real (OpenAI,
Voyage etc., respeitando a dimensão 1536 da coluna ou migrando-a) e implementar
`EmbeddingProvider` sobre ele. Até lá, a busca funciona mecanicamente mas não é
semântica de verdade — não usar `HashingEmbeddingProvider` fora de dev/teste.

---

## D-27 · `search_knowledge` fica de fora do registro sem embeddings reais · S08

**Contexto.** `app/agent/tools/registry.py` monta o `ToolRegistry` que o worker instala
no motor. `search_knowledge` (S08) depende de um `EmbeddingProvider`, e D-26 proíbe usar
`HashingEmbeddingProvider` fora de dev/teste.

**Alternativa descartada.** Usar `HashingEmbeddingProvider` em qualquer ambiente até um
fornecedor real existir. Contraria D-26 diretamente: a tool responderia com "relevância"
vinda de hash de palavra, não de similaridade semântica — pior do que a tool não existir,
porque o guardrail de saída (11.5) trata resultado de tool como fonte de verdade.

**Decisão.** `build_registry(embeddings=...)` aceita `embeddings=None` e, nesse caso,
registra só `list_services` (que não depende de embeddings). `app.workers.base` só passa
um `HashingEmbeddingProvider` quando `settings.environment` é `local` ou `test`; fora
disso, `search_knowledge` fica ausente do catálogo de tools e um aviso é logado — o
motor continua funcionando com o resto do conjunto.

**Consequência.** Quando um `EmbeddingProvider` real existir, `_embedding_provider` em
`app/workers/base.py` é o único ponto a trocar; nada no motor, no registro ou na tool
muda.

---

## D-28 · O rate limit fica no worker; o guardrail só declara o contrato · S09

**Contexto.** A seção 11.5 lista rate limit como guardrail de entrada, e a S05 já tinha
implementado `ContactRateLimiter` (Redis) dentro de `flush_conversation`, antes de o turno
existir.

**Alternativa descartada.** Mover a contagem para dentro de `InputGuardrail`, no motor.
Ficaria mais coeso no papel, mas o flood passaria a carregar config do tenant e o estado
da conversa **antes** de ser descartado — duas idas ao banco por mensagem de flood,
exatamente no cenário em que se quer gastar menos.

**Decisão.** O contador continua em `app/workers/ratelimit.py`, chamado no ponto mais
barato. `app/agent/guardrails.py` declara o protocolo `TurnRateLimiter` e aceita um
limiter opcional; em produção (`workers/base.py`) ele vai vazio, porque o worker já
contou. O campo existe para os caminhos que não passam pelo worker de entrada: o runner
conversacional (S11) e o widget web (S21).

**Consequência.** Quem lê `guardrails.py` sozinho vê o rate limit como contrato, não como
implementação. O teste do anti-flood continua em `tests/unit/test_worker_settings.py` e o
do protocolo, em `tests/unit/test_guardrails.py`.

---

## D-29 · Base de conhecimento e horário de funcionamento também são evidência · S09

**Contexto.** A 11.5 diz que um horário na resposta precisa vir de `check_availability`
ou `confirm_appointment` no mesmo turno.

**Problema.** Lido ao pé da letra, o agente fica proibido de responder "atendemos das 8h
às 19h" — que é FAQ, não agenda, e é uma das perguntas mais comuns no balcão. A resposta
certa seria descartada, regenerada e, na segunda, viraria handoff.

**Decisão.** `Evidence` tem três conjuntos separados: `slots` (pares data+hora que saíram
juntos de um resultado de tool), `times` e `dates` (referências soltas legítimas — RAG,
catálogo, e as **bordas** das janelas de `business_hours`). Uma promessa de agenda exige
`slots`; uma citação de horário de funcionamento se resolve com `times`. O meio da janela
nunca entra: é exatamente ali que mora a vaga inventada.

**Consequência.** A distinção entre "informar" e "prometer" é feita por linguagem de
oferta/confirmação (`_OFFER` em `guardrails.py`) mais a regra de que hora vinda de slot
real sempre é conferida como par. Toda folga nova nessa fronteira precisa de um teste em
`tests/unit/test_guardrails.py` que prove os dois lados — o caso que passa e o que não.

---

## D-30 · Detecção de prompt injection registra, não bloqueia · S09

**Contexto.** A 11.5 pede detecção de tentativa de prompt injection no conteúdo recebido.

**Alternativa descartada.** Curto-circuitar a conversa quando o detector marca. Daria a
qualquer pessoa um jeito trivial de se negar atendimento — "ignora o que eu falei antes"
é português comum — e trocaria uma defesa que funciona por uma que só parece funcionar.

**Decisão.** A defesa contra injeção é estrutural e já existe: conteúdo de terceiro entra
delimitado em `<dado>` (`app.agent.prompt.wrap_user_content`, invariante 7) e o bloco
`[LIMITES INEGOCIÁVEIS]` diz ao modelo que ali dentro é dado. `detect_injection` devolve
as marcas encontradas, que vão para log e para `InputDecision.injection_flags` — métrica
e trilha, não filtro.

**Consequência.** A métrica de tentativas de injeção por tenant (S22) sai daí. Se algum
dia houver ação automática sobre a marca, ela precisa de um critério muito mais estreito
do que o detector atual.

---

## D-31 · Resposta quebrada vira N mensagens, com adiamento crescente · S09

**Contexto.** O guardrail de saída quebra a resposta por `persona.max_message_chars`
(11.5). Até a S09, o turno gravava uma linha em `messages` e publicava um job de saída.

**Problema.** A fila do arq não promete ordem entre jobs publicados no mesmo instante, e
o backoff de retentativa pode reordenar. Uma resposta quebrada chegando fora de ordem é
pior do que uma mensagem longa.

**Decisão.** `TurnOutcome.messages` carrega as partes; `persist_turn` grava **uma linha
por parte** (o painel mostra o que a pessoa recebeu) e o handler publica um job por parte
com `defer_seconds = índice * outbound_part_delay_seconds` (default 1.5s). Tokens e custo
vão só na primeira linha: são do turno, não da mensagem, e repeti-los multiplicaria o
número que o disjuntor lê para decidir escalar.

**Consequência.** A ordem passa a depender do relógio do Redis, não da sorte. O efeito
colateral — as mensagens chegando com alguns segundos entre si — é o comportamento
desejado no WhatsApp. `OutboundQueue.enqueue_outbound` ganhou `defer_seconds`.

---

## D-32 · Handoff tem uma porta de entrada só; `escalate_to_human` so sinaliza · S10

**Contexto.** RF-26 lista cinco origens de escalada: pedido explícito, gatilho de risco,
baixa confiança repetida, falha de ferramenta e tópico fora de escopo. As duas primeiras
já casam no guardrail de entrada (S09, `match_trigger`); as outras três só o próprio
modelo percebe, no meio do turno.

**Alternativa descartada.** Fazer a tool `escalate_to_human` abrir a linha em `handoffs`,
silenciar a conversa e notificar o responsável ela mesma, dentro do handler da tool.
Funcionaria, mas duplicaria a lógica de idempotência da notificação (RF-27, "enviada uma
única vez") em cada origem — a tool, o disjuntor de custo/mensagens (11.5) e o teto de
iterações teriam cada um seu próprio caminho para a mesma tabela.

**Decisão.** `escalate_to_human` (`app/agent/tools/escalate_to_human.py`) só devolve
`{"status": "escalated", "category": ..., "reason": ...}` — nunca toca banco. O motor
(`app.agent.engine._tool_escalation_reason`) lê esse resultado e marca
`TurnOutcome.escalate = True` com `escalation_reason = "tool:<motivo>"`, do mesmo jeito
que já fazia para o disjuntor e o teto de iterações. `app.agent.runner.AgentTurnHandler`
é a única porta que chama `HandoffService.open` — depois que o turno inteiro terminou,
com o resumo e o canal de notificação já resolvidos. Silêncio e retomada (RF-28) seguem
o mesmo padrão: `HandoffService.should_run_turn` é o único lugar que decide se o motor
roda, lido a partir de `conversations.status`/`silenced_until` que `load_state` já traz.

**`triggered_by` é derivado do prefixo do motivo, não plumbado à parte.**
`app.agent.handoff.triggered_by_for` mapeia `"tool:"` → `agent` (o modelo decidiu),
`"trigger:"` → `contact` (a pessoa escreveu algo que casou um gatilho de config), e o
resto (disjuntor, teto de iterações, segunda falha do guardrail de saída) → `agent` ou
`rule` conforme o caso. Evita adicionar um campo novo a `TurnOutcome` só para isso.

**Retomada por timeout é preguiçosa, não um cron.** `should_run_turn` fecha o handoff e
libera o turno **quando a próxima mensagem chega**, não num job varrendo conversas. Mais
simples e suficiente: RF-28 só promete "o agente volta a responder" — nada exige que o
`status` mude no banco antes de alguém escrever de novo. Se o painel (S17) precisar
mostrar a conversa como "ativa" mesmo sem mensagem nova, um cron de varredura entra
depois, sem mudar `should_run_turn`.

**Só WhatsApp notifica de verdade.** `escalation.notify` aceita `whatsapp`, `email` e
`panel` (S03), mas não existe provedor de email nem painel (S17) ainda.
`WhatsAppHandoffNotifier` ignora os outros dois com log, sem falhar a abertura do
handoff — RF-27 fica coberto pelo canal que toda config de exemplo já usa.

---

## D-33 · O modelo da suíte conversacional é roteirizado, não ao vivo · S11

**Contexto.** A 19.4 manda rodar a suíte inteira a cada alteração de prompt e **bloquear
o merge na queda de qualquer cenário**. Isso exige um placar em que "caiu" signifique
"alguma coisa quebrou", e não "o modelo respondeu diferente hoje".

**Alternativa descartada.** Rodar os cenários contra a API de verdade e julgar tudo com
juiz LLM. Cada execução custaria dinheiro, levaria minutos, precisaria de chave no CI e —
o que mata a ideia — daria resultados diferentes para o mesmo código. Um placar que pisca
não bloqueia merge: ensina a reexecutar até passar.

**Decisão.** O cenário traz o roteiro do modelo (`agent: [{say|call}]`) e
`ScriptedModel` o devolve na ordem. O que a suíte mede é o **sistema em volta do
modelo** — curto-circuito de gatilho, despacho de tool, conferência de saída,
escalonamento, silêncio de handoff. Tudo isso é código nosso e é determinístico.

**Consequência boa.** Dá para roteirizar o modelo **errando de propósito**, que é o teste
que a API real não permite fazer de forma confiável: `preco_inventado` e
`horario_inventado` fazem o modelo citar valor e horário que não vieram de ferramenta
nenhuma, e provam que a conferência da 11.5 descarta a resposta antes de o cliente vê-la.

**O que fica de fora.** A qualidade da decisão do modelo (quando chamar tool, o que
perguntar) não é medida offline. Para isso existe `CONVERSATIONAL_LIVE=1`, que troca o
roteiro pelo `AnthropicProvider` — e não entra no placar.

---

## D-34 · O placar versiona o número de asserções, não só o status · S11

**Contexto.** "Queda em qualquer cenário bloqueia o merge" (19.4) protege contra o
cenário ficar vermelho. Não protege contra o caminho mais fácil de ficar verde: apagar a
asserção que incomoda. O cenário continua listado, continua passando, e não confere mais
nada.

**Decisão.** `tests/conversational/placar.yaml` guarda, por cenário, `status` **e**
`assercoes`. `test_placar.py` exige igualdade: menos asserções do que o registrado é
enfraquecimento e falha; mais pede atualização do placar. Somado às outras conferências
(cenário fora do placar, linha sem cenário, queda de cenário aprovado), o conjunto fecha
as maneiras de conseguir verde sem ter conferido.

**`known_failure` com motivo obrigatório.** Defeito conhecido fica rastreado em vez de
apagado, e **tem** de continuar falhando: quando passar, a suíte manda promover a linha.
Dois estão registrados hoje, ambos encontrados pela própria suíte (placeholder `{address}`
não resolvido na mensagem de emergência; pedido de humano que escala sem responder nada
ao cliente).

---

## D-35 · A suíte conversacional não toca banco nem rede · S11

**Contexto.** O critério de aceite da S11 é `pytest tests/conversational` rodando
offline. As tools de leitura de verdade (`list_services`, `search_knowledge`) consultam
Postgres; `HandoffService` grava em `handoffs`; o canal de saída fala com a Graph API.

**Decisão.** O runner monta o registro de tools com o **schema de produção e o handler
trocado** (`dataclasses.replace(spec, handler=...)`): nome, descrição e `input_schema`
continuam vindo da tool real — que é o que o modelo lê para decidir —, e os dados vêm do
YAML do cenário. Handoff e canal viram `InMemoryHandoffs` e `FakeChannel`, que guardam só
o que muda o turno seguinte (um handoff aberto por vez, silêncio até `silenced_until`).
A persistência de verdade já tem teste contra banco real em `tests/integration/`.

**Amarrado por teste.** `test_nenhum_cenario_toca_o_banco` troca `tenant_session` e
`search_knowledge` por funções que levantam, e roda todos os cenários. No dia em que
alguém registrar no runner uma tool que ainda consulta o banco, esse teste cai.

---

## D-36 · Cenário declara `now` congelado e slots relativos · S11

**Contexto.** Os guardrails de saída interpretam data e hora em relação a *hoje*
(`find_mentions(text, today=...)`), e as janelas de `business_hours` variam por dia da
semana. Um cenário com data absoluta (`2026-09-09`) começa a testar o passado assim que o
dia chega — e passa a falhar por motivo que não é o dele.

**Decisão.** O relógio do cenário é congelado (padrão: terça, 08/09/2026, 10h em São
Paulo, dia útil dentro do horário de funcionamento do tenant de exemplo) e todo slot é
escrito relativo (`+1 14:30`, `hoje 16:00`), resolvido pelo `FakeCalendar` contra esse
instante. Nenhum cenário chama `now()`.

**Tool que ainda não existe entra como stub do cenário.** `check_availability` e
`confirm_appointment` são citadas pela 19.3 mas só ganham implementação na S13/S15.
Declarar o schema delas no YAML (e não em Python) evita fixar agora uma interface que
ainda não foi desenhada; quando existirem, o cenário troca o stub pela tool de verdade.

---

## D-37 · A URL sincrona nomeia o driver; o default do SQLAlchemy nao serve · S11

**Contexto.** `Settings.sync_database_url` era a URL async com o sufixo `+asyncpg`
removido, o que produzia `postgresql://...`. Quem consome isso e o Alembic (migrations,
inclusive no deploy) e os testes de integracao.

**O que aconteceu.** `pyproject.toml` pede `sqlalchemy[asyncio]>=2.0.36` sem teto. O CI
instala do zero e recebeu **2.1.1**; o venv local esta em **2.0.52**. No 2.1 o driver
default de `postgresql://` mudou de `psycopg2` para `psycopg` (v3) — e a dependencia
declarada e `psycopg2-binary`. Resultado: `alembic upgrade head` morreu com
`ModuleNotFoundError: No module named 'psycopg'` no CI. O mesmo comando roda no deploy.

**Decisao.** A URL sincrona passa a nomear o driver por extenso
(`postgresql+psycopg2://`), trocando o esquema inteiro em vez de remover o sufixo — isso
cobre tambem a URL que chega do ambiente sem driver nenhum, que e o caso comum em deploy.
`test_url_sincrona_nomeia_o_driver` fixa as duas formas de entrada.

**Por que nao pinar o SQLAlchemy em vez disso.** Pinar resolveria este sintoma e esconderia
a causa: a URL continuaria dependendo de um default. Nomear o driver e correto em qualquer
versao. **A divergencia de versao entre CI e ambiente local continua aberta** — ela e
politica de dependencia (teto nas versoes ou lock file), nao conserto de CI, e merece
decisao propria. Este episodio e o argumento a favor: o primeiro sintoma foi uma anotacao
de tipo faltando, o segundo foi o deploy quebrado.

---

## D-38 · Endereço e telefone da unidade são campos de `identity`, e placeholder sem valor não sai · S25

**Contexto.** O `reply` do gatilho de emergência cita `{address}` e `{phone}`;
`messages.confirmation` cita `{address}` de novo, além dos campos do agendamento. Nada
preenchia isso: o motor chamava `InputGuardrail.check` sem `placeholders` e, mesmo que
passasse, não havia de onde tirar os valores — `identity` tinha só `name`, `vertical` e
`about`. A pessoa com o animal atropelado no colo lia `{address}` na tela (DEF-01).

**Decisão.** Três camadas, em `app/domain/templates.py`:

1. **Nome validado.** `KNOWN_PLACEHOLDERS` é a lista do que o sistema sabe preencher, e a
   config de tenant recusa texto que cite nome fora dela. Um `{endereco}` escrito em
   português nunca teria quem o preenchesse; descobrir isso no onboarding é de graça perto
   de descobrir na primeira emergência. A conferência é do **nome**, não do valor — estática,
   imune à expansão de `${VAR}`.
2. **Valor.** `identity.address` e `identity.phone`, genéricos (unidade que atende, em
   qualquer vertical) e validados: endereço curto demais, telefone com menos de 8 dígitos ou
   campo que contenha outro placeholder são erro. `TenantConfig.template_values()` monta o
   mapa e `AgentEngine._input_guardrail` o passa, completado com o nome do contato. É o
   mesmo mapa que a confirmação de agendamento (S15/S16) vai usar, só faltando os campos do
   agendamento.
3. **Rede de segurança.** `drop_unresolved` roda dentro de `split_message`, por onde passa
   **toda** mensagem que vai ao cliente — curto-circuito de gatilho, resposta aprovada,
   substituição por recusa e fallback do motor. Um caminho novo que esqueça de preencher
   continua coberto, que é exatamente o que faltou aqui. O descarte é por frase: "va direto
   a clinica: {address}." sai inteira, porque apagar só o campo prometeria um dado que não
   veio. Se não sobrar nada, o campo é apagado e o resto fica — mensagem torta é ruim,
   silêncio num gatilho de emergência é pior.

**A tolerância de `_safe_format` fica.** Ela agora vive em `templates.fill`, com o mesmo
motivo: `KeyError` ali deixaria a pessoa sem resposta nenhuma. Ela deixa de ser a última
palavra porque a camada 3 existe.

**Campo vazio fica fora do mapa, não vira string vazia.** Assim o placeholder continua sem
resolver e a frase é descartada, em vez de o cliente receber "va direto a clinica: .".

**Onde a falta do campo é erro fatal, e onde não é.** `load_tenant_config` (arquivo em git,
CI, `--validate` do onboarding) recusa config que cite `{address}` com o campo vazio:
falhar ali é de graça e o modo de falha é visível. O caminho de produção —
`tenants.config` + `resolved()`, que roda a cada turno em `app.domain.tenant_registry` —
**não** levanta por isso: um `${VAR}` ausente no ambiente viraria "config inválida" e
calaria todas as conversas do tenant, que é pior do que o defeito sendo corrigido. Ali quem
cobre é a camada 3.

**Multi-unidade continua sendo tema da S12.** Quando `resources` existir, o endereço por
recurso entra no mapa de placeholders do turno e cobre o da unidade, sem mudar quem cita
nem quem valida.

---

## D-39 · Gatilho que escala sem `reply` usa aviso padrão do código, não exigência de schema · S25

**Contexto.** `pedido_humano` casa por `match_intent: ask_for_human` e não declara `reply`.
A mensagem genérica do tenant só entrava em gatilho que **não** escala, então o handoff
abria, a equipe era notificada e o cliente não recebia nada. Pior do que parece: por RF-28
o agente também ignora as mensagens seguintes enquanto o silêncio do handoff durar, então a
pessoa repete o pedido no vazio e desiste (DEF-02).

**As duas saídas.** Exigir `reply` em gatilho que escala, no schema de tenant; ou um aviso
padrão em código para gatilho que escala sem texto.

**Decisão: aviso padrão em código** (`DEFAULT_HANDOFF_NOTICE`, em
`app/agent/guardrails.py`), com a redação vindo de `messages.handoff_notice` quando o tenant
a escrever. A config manda no texto; o código garante que exista um.

**Por que não a exigência de schema.** Escalar sem avisar a pessoa não é uma preferência de
cliente, é comportamento inaceitável do produto — e garantia de produto não se delega a
quem preenche YAML. A exigência de schema ainda deixaria dois furos: ela quebra as configs
já publicadas no deploy seguinte (inclusive `_template.yaml`, que traz `pedido_humano` sem
`reply`) e não cobre o caso em que o `reply` existe mas não sobra texto dele — um `reply`
que só cita placeholder sem valor volta a ser escalada muda. O código cobre os três: gatilho
sem `reply`, `reply` vazio e `reply` que fica sem texto útil depois de `drop_unresolved`
caem todos no aviso.

**Recusa continua caindo em `messages.out_of_scope`.** Ali não há garantia a dar: recusar
sem texto configurado não abandona ninguém, só devolve o turno para o modelo. A distinção
entre recusa e escalada fica mais nítida, não mais fraca — são dois textos diferentes.

**Nada específico de cliente entrou no código.** O aviso não nomeia negócio, vertical nem
pessoa; é a frase mínima que qualquer tenant precisaria dizer.

---

## D-40 · O endereço da unidade entra no prompt, mas não é evidência · S25

**Contexto.** Com `identity.address` e `identity.phone` existindo (D-38), "onde vocês
ficam?" e "qual o telefone?" continuavam sem resposta: endereço não vem de tool nenhuma e
não está na base de conhecimento. A saída do agente era inventar ou escalar uma pergunta
banal — e inventar endereço custa o mesmo que inventar horário, porque a pessoa vai até o
lugar.

**Decisão.** Os dois campos entram no bloco `[IDENTIDADE]`, com a instrução de informá-los
exatamente como estão e não completar o que não está escrito (bairro, referência, ramal,
outro telefone). São fato da config e entram sem `<dado>`, como `identity.name` e
`identity.about` — não são conteúdo de usuário. Campo vazio não gera linha: o que não está
escrito continua caindo em "nunca invente dados" do bloco de limites, e nenhum tenant é
obrigado a preencher.

**Endereço não é evidência.** D-29 vale para hora, data e preço, e continua valendo só para
isso. A conferência mecânica do endereço é ele ter saído da config, não o guardrail de
saída. **Consequência aceita:** endereço que contenha algo com cara de relógio ("sala 8h")
é descartado pela regra de hora sem evidência, e o turno é regenerado. Afrouxar a regra de
hora para caber endereço trocaria o risco de maior impacto do projeto (seção 21) por uma
conveniência de cadastro; quem tiver esse endereço escreve `sala 8` no campo.

**Amarrado por cenário.** `endereco_da_unidade` prova as duas metades: a resposta sai com o
endereço, sem tool e sem handoff, e sobrevive ao guardrail de saída. Se um dia o número da
rua passar a ser lido como horário inventado, é esse cenário que cai.

---

## D-41 · O refresh token fica atrás de um `SecretStore`; a cifra é do secret manager · S12

**Contexto.** A 14.3 pede refresh token "cifrado em secret store, referenciado por
`providers.credentials_ref`". Não existe secret manager escolhido nem biblioteca de
criptografia nas dependências, e inventar um esquema de cifra próprio (chave mestra na
env, tokens cifrados no banco) deixaria o token no banco — que é o que a seção 18.1 e o
comentário de `Provider.credentials_ref` proíbem.

**Decisão.** `app.core.secrets.SecretStore` é um Protocol de uma operação
(`get(ref) -> str`). `credentials_ref` é só o ponteiro; o token nunca é persistido nem
logado, e a mensagem de `SecretNotFound` carrega o ponteiro, não o valor.
`EnvSecretStore` (`env:NOME`) serve ao desenvolvimento e a deploys simples; o secret
manager real entra como outra implementação, sem tocar em `calendar/`.

**Fora do escopo da S12.** O fluxo de consentimento (URL de autorização e troca do `code`
pelo refresh token) é do onboarding (S23): é ele que **grava** no secret store. A S12 só
**consome** o token. Sem `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` o provider não troca
token — falha com `CalendarAuthError`, não com erro opaco.

---

## D-42 · Insert idempotente por `409` + busca por `iCalUID`; `freebusy` falha fechado · S12

**Insert.** O Google recusa um segundo `events.insert` com o mesmo `iCalUID` (`409`).
Tratamos o `409` como "já existe": buscamos por `iCalUID` e devolvemos o evento. É isso que
torna seguro retentar um insert cuja resposta se perdeu (503 *depois* de gravar), caso em
que a primeira tentativa funcionou. O teste usa um calendário falso **com estado**, para
afirmar sobre o calendário (1 evento), não sobre o número de chamadas. `409` com o evento
cancelado é `CalendarPermanentError`: ressuscitar evento apagado por outra pessoa não é
decisão deste módulo.

**`freebusy`.** Calendário que a resposta não traz, ou traz com `errors`, **não** some do
resultado nem vira lista vazia: levanta `CalendarQueryError` (com o que respondeu em
`partial`). Tratar silêncio como "livre" é inventar disponibilidade (invariante 1).
Lotes de até 50 calendários por chamada; cache de 60s por `(calendário, janela)` **na
instância** — uma por `credentials_ref`, para não misturar tenants — e invalidado por
qualquer escrita nossa naquele calendário, senão o "livre" antigo sobreviveria ao evento
que acabamos de criar. Cache em processo: com N workers, cada um tem o seu; para uma janela
de 60s isso é aceitável e evita um Redis a mais no caminho crítico.

---

## D-43 · Retentativa: o que retenta, o que não, e para onde vai o resto · S12

| Resposta | Comportamento |
|---|---|
| `5xx`, `429`, `403` com `rateLimitExceeded`/`userRateLimitExceeded`, falha de transporte | backoff exponencial com jitter (`Retry-After` tem precedência) até `google_max_tries`; esgotado → `CalendarSyncPending` |
| `401` | renova o access token **uma vez** e repete; segundo `401` → `CalendarAuthError` |
| `invalid_grant` na renovação | `CalendarAuthError` na hora — só o cliente refazendo o consentimento resolve |
| outros `403`/`4xx` | `CalendarPermanentError` (`404` → `CalendarNotFound`), sem retentativa |
| `404`/`410` em `delete` | sucesso: o estado desejado (evento inexistente) é o que existe |
| `410` em `sync_changes` | descarta o token e relê do zero, devolvendo `full_sync=True` (13.5) |

`app.calendar.sync_state.push_appointment_event` traduz o desfecho em
`appointments.sync_status`: `synced`, `pending_sync` (transitório persistente, com log
`error` — o alerta ao operador da 14.3) ou `sync_failed` (auth/permanente, precisa de
humano). Nunca propaga a falha: o agendamento já está no banco e o banco é a fonte de
verdade. **Chamar fora da transação do advisory lock**, para não segurar o profissional/dia
durante dezenas de segundos de retentativa por causa de uma API de terceiros.

**Em aberto, de quem usa.** S14 decide *quando* chamar `push_appointment_event` e o texto
com que o agente avisa "confirmo em instantes". O job que reprocessa `pending_sync` não
existe ainda (cabe em S19/S20, junto do cron).

---

## D-44 · O slot é o horário do atendimento; os buffers ocupam, mas não se gravam · S13

**Contexto.** A 13.2 diz "candidato válido se `[t, t+duration_total]` ⊆ janela", sem dizer
se `t` é o início do atendimento ou do bloco com buffers.

**Decisão.** O slot oferecido (`Slot.start`/`end`) é o **atendimento**: é o que a pessoa
lê e o que vai para o calendário. O bloco que precisa estar livre e dentro da janela é
`[start - buffer_before, start + duração + buffer_after)` (`ServiceDuration.block_for`).
A grade de passos parte do início da janela e anda sobre o horário do atendimento, então a
oferta sai em hora redonda (08:15, não 08:10).

**Contrato para a S14.** `appointments` e `holds` gravam o horário do atendimento, não o
bloco. Ao ler a ocupação, o motor estende cada linha pelos buffers **do serviço dela**
(join em `services`): o buffer de um atendimento já marcado protege o próximo.
Consequência: `appt_no_overlap` protege o atendimento, não o buffer — buffer é regra do
motor, não da rede final. Se um dia precisar ser rede final, a constraint passa a comparar
um `tstzrange` estendido, e não muda o que se grava.

---

## D-45 · Borda de janela em horário de verão: o primeiro instante em que o relógio marca a hora · S13

`app.scheduling.rules.local_to_utc` resolve as duas anomalias com uma regra só. Hora
repetida (recuo): a primeira ocorrência. Hora que não existe (salto): o instante da
transição — "abre às 02:30" num dia em que 02:00 vira 03:00 abre às 03:00, e "fecha às
02:30" fecha às 03:00. A janela nunca ganha a hora que não existiu (invariante 1).

Os candidatos andam em **tempo absoluto** a partir da borda já convertida: no salto,
nenhum cai na hora inexistente; no recuo, a hora repetida aparece duas vezes, como dois
instantes distintos (dedup é por instante UTC, não por hora local). São Paulo não tem mais
horário de verão desde 2019, mas os testes usam 2018/2019 de São Paulo (meia-noite que
pulava; 23h que repetia) e 2026 de Nova York — o código não sabe de país nenhum.

---

## D-46 · Formato de `working_hours`, `24:00` e jornada que atravessa a meia-noite · S13

- `providers.working_hours` usa o formato da 10.4 (`weekly` + `exceptions`), validado pelo
  mesmo `BusinessHours` do YAML; `timezone`, se vier, é ignorado — a timezone é da unidade.
- **`{}` segue a unidade** (é o que os seeds já gravam). `{"exceptions": [...]}` sem
  `weekly` também: são as férias de quem segue o horário da casa. Com `weekly`, dia ausente
  é dia sem trabalho.
- Horário de funcionamento: `locations.business_hours` quando não vazio; senão o
  `business_hours` do YAML do tenant. A timezone vem de `locations.timezone`, ou do YAML
  para profissional sem unidade.
- `24:00` vale como **fim** de janela (o schema já aceitava pelo padrão `HH:MM`; o motor é
  quem interpreta). Janelas que se encostam — inclusive `22:00-24:00` de um dia e
  `00:00-02:00` do seguinte — viram uma só antes da varredura, então um atendimento de 3h
  às 23h num turno 22h-02h cabe. O dia do slot é o **dia local** do início, nunca o UTC.

---

## D-47 · Recurso escasso: `service_resources` é lista de alternativas, com capacidade por pico · S13

Um serviço ligado a N recursos precisa de **um** deles (`holds`/`appointments` têm um
`resource_id` só). O primeiro livre, na ordem de nome, vai no `Slot.resource_id` para o
hold usar. "Livre" é: nenhum bloqueio no calendário do recurso e pico de usos simultâneos
dentro do bloco menor que `capacity` (dois usos em sequência numa sala de capacidade 1 não
a lotam). O `freebusy` não diz quantos lugares estão tomados, então intervalo ocupado no
calendário do recurso bloqueia a capacidade inteira. O recurso também precisa estar livre
durante os buffers (preparo e limpeza ocupam a sala).

Recurso de outra unidade não serve ao profissional — exceto em serviço `online`, que
ignora `location` (13.3). Fora isso, `online` não muda a disponibilidade: jornada e
horário de funcionamento valem igual; o link de videochamada no evento é da S15.

---

## D-48 · Preferência ordena, não filtra; falha de calendário fecha por profissional · S13

**Preferências** (`Preferences`: turnos, dias da semana, faixa de horário, instante de
referência) contam pontos de aderência; a ordem é (aderência, distância ao instante de
referência — `now` se não houver). Preferência impossível não zera a oferta: o agente
recebe os mais próximos e explica (cenário "preferência impossível", 19.3). Dois
profissionais livres no mesmo instante: fica o de melhor aderência, depois o primeiro na
ordem de nome. "Mais horários" (RF-17) é `offset` sobre a mesma ordenação.

**Falha fechada por dono** (estende D-42). `CalendarQueryError` tira só os calendários que
falharam; `CalendarSyncPending`/`CalendarAuthError` tiram o lote daquele cliente;
`calendar_id` sem cliente resolvido é erro de configuração e também tira. Quem saiu volta
em `AvailabilityResult.unavailable` com o motivo — o agente deve ler isso como "não
consegui consultar", nunca como "não há vaga". Profissional sem `calendar_id` (agenda
própria) conta só com o banco.

**Fora da S13.** `slot_token` assinado é da S15, junto com a tool `check_availability`
que embrulha `find_availability`; o `CalendarResolver` (qual cliente Google atende qual
`credentials_ref`, e qual atende o calendário de um recurso) é decidido por quem monta a
tool.

---

## D-49 · Advisory lock por (dono, dia UTC), em todos os dias que o bloco toca · S14

A 13.4 pede `pg_advisory_xact_lock(hashtext(provider_id::text || date))`. Detalhes que a
spec não fixa:

- **Dono:** profissional **e** recurso. Sem a chave do recurso, dois profissionais
  disputando a mesma sala em locks diferentes passariam juntos pela checagem.
- **Dia:** o dia UTC, não o da unidade — é só partição do lock, não regra de negócio. O
  bloco (atendimento + buffers) trava **todos** os dias que toca: dois blocos que se
  sobrepõem sempre compartilham uma chave, inclusive atravessando a meia-noite.
- **Ordem:** todas as chaves numa instrução só, deduplicadas e em ordem de `hashtext`.
  Quem precisa de chaves em comum as pega na mesma ordem, então não há deadlock entre
  pedidos — nem quando `hashtext` colide para chaves diferentes (colisão só serializa a
  mais).

O lock é tomado **antes** da checagem de sobreposição, no hold e na confirmação. Em READ
COMMITTED, a leitura feita depois de ganhar o lock já enxerga o que o dono anterior
gravou, porque ele só solta o lock no commit.

---

## D-50 · O hold não consulta o calendário nem recebe duração; a confirmação confere de novo · S14

**Hold.** O fim do atendimento vem do banco (`duration_override_min` ou a duração do
serviço) — quem chama manda só o início. A checagem é a do motor (`db_occupation`, com os
buffers de D-44), contra agendamentos ativos e holds vigentes. **Não** chama o `freebusy`:
o calendário já foi consultado na oferta, e rede dentro do lock seguraria o profissional
durante a retentativa (D-43). Antecedência mínima e horizonte também ficam na oferta; o
hold recusa só horário no passado. A garantia de que o horário saiu do motor é o
`slot_token` assinado (S15).

**Confirmação**, nesta ordem: (1) `SELECT ... FOR UPDATE` no hold; (2) procura agendamento
com `idempotency_key = sha256("hold:" + hold_id)` — achou, devolve o mesmo id, mesmo com o
hold já consumido ou vencido; (3) hold não ativo ou vencido → `hold_expired`; (4) lock e
checagem de novo, sem contar o próprio hold — pega o agendamento feito pelo painel em cima
de um hold (inclusive só no buffer, que a constraint não vê); (5) `INSERT` num
**savepoint**. Violação de `appt_no_overlap` (pelo nome da constraint ou pelo SQLSTATE
`23P01`) vira `{"status": "slot_taken"}` e o hold é marcado `expired`. O `FOR UPDATE`
antes da chave é o que faz duas confirmações simultâneas do mesmo hold darem uma linha só:
a segunda espera a primeira e já encontra a chave.

---

## D-51 · O agendamento nasce `pending_sync`; o evento é de quem chama, depois do commit · S14

`confirm_appointment` não fala com o Google. O agendamento entra com
`sync_status = 'pending_sync'` e quem chama (a tool da S15) faz
`push_appointment_event` **depois do commit**, fora do lock (D-43), que move para `synced`
ou `sync_failed`. Se o processo morrer entre o commit e o push, a linha já diz que falta
sincronizar — é o que o job de reprocessamento (S19/S20) vai procurar. Nascer `synced`
(o default da coluna) mentiria nesse intervalo.

---

## D-52 · A expiração de hold entrou na S14, como limpeza · S14

O `cron.py` previa a expiração na S19; o prompt da S14 a pede. `expire_holds_job` roda a
cada minuto e marca `expired` todo hold ativo vencido, de todos os tenants
(`bypass_rls_session`, D-03). É **limpeza, não correção**: toda leitura de ocupação já
ignora hold com `expires_at <= now`, então um cron parado não prende horário — só deixa o
índice parcial crescer e o painel mostrar reserva morta.

---

## D-53 · `testcontainers` instalado; o `redis` 8 que veio junto mudou um stub · S14

O teste de concorrência (aceite da S14, 19.2) sobe um Postgres próprio com
`testcontainers`, que estava nas dependências de dev mas não no venv. Instalar
`testcontainers[postgres,redis]` resolveu `redis` para 8.1 — dentro do `redis>=5.2` do
`pyproject.toml`, e é o que uma instalação limpa no CI também pega. O stub novo já tipa
`llen` do cliente async como awaitable, e o `cast` de `ConversationAggregator.pending`
virou `redundant-cast` no `mypy --strict`. O `cast` ficou, com
`# type: ignore[redundant-cast, unused-ignore]`, que vale nas duas faixas de versão.

---

## D-54 · `slot_token`: o corpo leva o horário; a conversa e o tenant entram na chave · S15

`app/scheduling/slot_token.py`. Formato `base64url(corpo JSON).base64url(HMAC-SHA256)`; o
corpo leva `provider_id`, `resource_id`, `service_id`, `starts_at` (UTC) e a validade. A
assinatura cobre os bytes do segmento **como foram transmitidos**, não o JSON reserializado.

**Tenant e conversa não vão no corpo — derivam a chave** (`HMAC(segredo, domínio | tenant |
conversa)`). O mesmo corpo assinado para outra conversa ou outro tenant tem outra
assinatura, então um token copiado de uma conversa para outra não vale, e não existe campo
de tenant para alguém adulterar. Efeito colateral aceito: "adulterado" e "de outra conversa"
dão o mesmo erro (`bad_signature`); o motivo só vai para o log (`slot_token_recusado`).

A assinatura é conferida **antes** de qualquer interpretação do corpo, e os dois segmentos
só aceitam o alfabeto do base64url: o decodificador do Python descarta caracteres soltos,
o que daria várias formas válidas do mesmo token.

**Validade:** `Settings.slot_token_ttl_minutes`, default 30. Curta de propósito: o
`hold_slot` não reconsulta o calendário (D-50), então uma oferta velha poderia reservar um
horário que o profissional já ocupou no Google. Cliente lento? O modelo consulta de novo —
uma leitura. A validade é do instante da **oferta**, não do horário do slot.

**Custo conhecido:** o token tem ~300 caracteres e o modelo o copia de volta. Um erro de
cópia vira `invalid_slot_token`, recuperável com nova consulta. Encurtar (UUID em binário)
é possível sem mudar o contrato, se a métrica mostrar recusas por cópia.

---

## D-55 · Instante ISO em resultado de tool é prova de horário; por isso não há `ends_at` · S15

O guardrail de saída (`app.agent.temporal.evidence_from_tool_result`) lê **todo** instante
ISO com offset de um `tool_result` como um horário que o modelo pode oferecer. Daí três
desvios do contrato da 11.4, todos para o mesmo fim:

- **Sem `ends_at` no slot.** O fim do atendimento das 18h30 seria prova de uma vaga às
  19h00 que não existe. O slot traz `duration_minutes`.
- **Sem `expires_at` no hold.** Um `expires_at` às 08:10 viraria prova de uma vaga às 08:10.
  O hold traz `expires_in_minutes`.
- Só o **início** do atendimento sai como instante ISO (`starts_at`), mais o `human`
  (`quarta-feira, 9 de setembro, às 14h`, que a gramática temporal lê de volta).

Regra para tool nova: instante ISO só para o que a pessoa pode ouvir como horário de
atendimento. `tests/integration/test_tools_scheduling.py` amarra: os instantes do resultado
são exatamente os `starts_at` oferecidos.

Também em `check_availability`: `offset` (o "mais horários" da RF-17, que o motor já
suportava); `limit` omitido vale `scheduling.slots_per_offer` e nunca passa dele; e
`next_available_date` é a data do primeiro horário devolvido — ou, se a janela está vazia,
a do primeiro depois dela até o horizonte do tenant (`null` se não há).

---

## D-56 · `hold_slot`: só o token; idempotente; troca em savepoint · S15

A tool aceita **um** argumento, `slot_token`. Não há `provider_id`, `service_id` nem
`starts_at` onde o modelo escrever um horário, e o que ele mandar além do schema é
descartado pelo Pydantic antes do handler. Token recusado volta como `invalid_slot_token`
**sem tocar o banco** (teste conta linhas em `holds`).

- **Idempotente entre turnos.** Mesma (conversa, profissional, serviço, início) com reserva
  vigente devolve a mesma reserva, sem estender o prazo — senão o "pode ser esse" do
  cliente receberia `slot_taken` por causa do hold dele mesmo.
- **Trocar de horário libera os anteriores da conversa, e a liberação é um savepoint.**
  Achado pelo teste: 08:00 e 08:15 se sobrepõem (30 min + buffer), então a reserva antiga
  da própria conversa bloqueava a nova. A liberação vem antes da tentativa e é desfeita se
  a tentativa falha (`slot_taken`): quem pediu a troca não perde o que tinha.
- `booking.confirm_appointment` (S14) ganhou `conversation_id` opcional. A tool o passa
  sempre: `hold_id` de outra conversa do mesmo tenant responde `hold_not_found`, igual a um
  id que não existe. Sem isso, qualquer um que obtivesse um `hold_id` o confirmaria.

---

## D-57 · `confirm_appointment`: banco primeiro, calendário depois, e a confirmação nunca perde o quando · S15

**Duas fases** (resolve o "em aberto" da D-43). (1) transação do banco: hold vira
agendamento sob o advisory lock; commit. (2) fora do lock, `push_appointment_event`.
Falha do calendário **não** vira erro da tool: o agendamento existe e dizer "não consegui"
faria o cliente tentar de novo ou desistir de uma reserva válida. `pending_sync` e
`sync_failed` ficam na linha para o job de reprocessamento (S19/S20); o modelo não os vê.
Exceção inesperada na fase 2 é engolida pelo mesmo motivo. Agenda própria (sem
`calendar_id`) nasce `pending_sync` e é movida para `synced`: não há espelho a manter.
Repetir a confirmação de agendamento não `synced` reenvia o evento (idempotente por
`iCalUID`); de agendamento `synced`, não toca o calendário.

**Falhas recuperáveis** (`hold_expired`, `slot_taken`, `hold_not_found`) voltam com
`is_error=True` e `suggested_action` no formato da 11.4. Não é detalhe: `next_stage` só
promove para `booked` tool que terminou bem, e o guardrail só lê horário de resultado que
não é erro. Um `hold_expired` com `is_error=False` faria o funil registrar uma venda.

**Subject:** resolve o subject do contato pelo nome (sem diferenciar caixa) quando há
exatamente um; **não cria**. O tipo (pet, dependente) só `save_contact_info` recebe, e
inventá-lo aqui gravaria dado errado. Sem subject cadastrado, o nome fica em
`intake.subject_name`.

**`intake`:** passa por `merge_collected` — só chaves que o tenant declara, valor vazio não
apaga o que o motor já coletou. `notes` mora em `intake._notes` (`appointments` não tem
coluna para observação).

**`confirmation_message`** (D-38, ponto 4) é `messages.confirmation` preenchido com os
campos do agendamento. Defeito encontrado ao construí-lo: o `confirmation` do tenant de
exemplo é escrito com `>`, que dobra tudo numa linha, e `drop_unresolved` descarta por
**frase** — um `{prep_line}` vazio apagava a mensagem inteira e sobrava `"Prontinho!"`, sem
data nem hora. A tool tenta o caminho normal e, se ele comeu a data ou a hora que o
template cita, refaz apagando só os campos opcionais vazios (preço, preparo, endereço).
Template com um campo por linha (`|`) continua perdendo só a linha vazia. O mecanismo da
S25 não foi alterado.

---

## D-58 · Resolver de calendário: um cliente por `credentials_ref`; recurso com calendário fica de fora · S15

`app/calendar/resolver.py` responde a pergunta que a D-48 deixou para quem monta a tool.
`GoogleCalendarResolver` guarda uma instância de `GoogleCalendarProvider` por
`credentials_ref` (o cache de `freebusy` e o token moram nela, D-42) sobre o cliente HTTP do
worker e o `EnvSecretStore`. Sem credencial, ou `calendar_provider != 'google'`: `None`, que
o motor lê como "não consegui consultar", nunca como "livre".

**`resources` não tem `credentials_ref`** (09) e a spec não diz com que conta o calendário
de uma sala é lido. Escolher uma por palpite pode devolver "livre" para o que está ocupado.
Decisão: recurso **com** `calendar_id` fica de fora (falha fechada, com aviso no log);
recurso sem `calendar_id` (só o banco conta) funciona. **Pendência:** quando existir o
vínculo (coluna `resources.credentials_ref` ou convenção), basta o resolver tratá-lo.

`build_registry(calendars=...)` aceita o resolver; sem ele, `no_calendars` — só agenda
própria aparece na oferta.

---

## D-59 · `save_contact_info` e `find_appointments` · S15

**`save_contact_info`:** o contato é o da conversa (`ctx.contact_id`), nunca argumento.
`attributes` é mesclado chave a chave (um turno que traz só a espécie não apaga a raça) e
fechado na forma — chave `[a-z][a-z0-9_]{0,39}`, valor escalar de até 200 caracteres, no
máximo 20 —, porque é o único lugar onde o modelo escreve JSON livre no banco. Documento,
CNPJ e cartão são **recusados sem gravar nada**, com a mesma detecção do guardrail de saída
(`redact_pii`, 18.2); só o tipo vai para o log, nunca o valor. O mesmo `subject` (tipo +
nome, sem diferenciar caixa) é atualizado, não duplicado, e o nome gravado na primeira vez
não é reescrito. Não toca `conversations.collected`: o motor o recalcula e o grava em
`persist_turn`, então uma escrita da tool seria sobrescrita.

**`find_appointments`:** sem argumentos. Futuros (`starts_at >= now`), ativos
(`confirmed`/`rescheduled`) e do contato da conversa, até 10, do mais próximo ao mais
distante. É de onde a S16 tira o `appointment_id`.

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

---

Estado ao fim da S09, contra Postgres 16 real:

| Item | Estado |
|---|---|
| Migrations `0001`–`0005` | ✅ aplicadas (`0005 (head)`) |
| Gramática temporal pt-BR | ✅ 82 testes novos |
| Guardrails de entrada e saída | ✅ 62 testes novos |
| Guardrails dentro do motor (regeneração e escalonamento) | ✅ 15 testes novos |
| Persistência da resposta quebrada (D-31) | ✅ verde contra banco real |
| Total: 422 testes, nenhum pulado | ✅ verde |
| `ruff check` + `ruff format --check` | ✅ limpo |
| `mypy --strict` | ✅ limpo (50 arquivos) |

---

Estado ao fim da S10, contra Postgres 16 e Redis reais:

| Item | Estado |
|---|---|
| Migrations `0001`–`0005` | ✅ aplicadas (`0005 (head)`) — nenhuma nova: `handoffs.summary`/`notified_at` e `conversations.silenced_until` já vinham da S02 (D-08) |
| `escalate_to_human` (tool) + `app.agent.handoff` (serviço) | ✅ 24 testes novos |
| Handoff ponta a ponta (silêncio, retomada por timeout, notificação única) | ✅ verde contra banco real |
| Total: 446 testes, nenhum pulado | ✅ verde |
| `ruff check` + `ruff format --check` | ✅ limpo |
| `mypy --strict` | ✅ limpo (52 arquivos) |


---

Estado ao fim da S11, contra Postgres 16 e Redis reais:

| Item | Estado |
|---|---|
| Runner da suíte conversacional (19.3) | ✅ 11 cenários, 44 testes novos |
| Placar versionado (19.4) | ✅ 9 cenários `pass`, 2 `known_failure` com motivo |
| `pytest tests/conversational` sem Postgres, Redis ou chave de API | ✅ verde, determinístico |
| Total: 490 testes (2 pulados: os `known_failure`) | ✅ verde |
| `ruff check` + `ruff format --check` | ✅ limpo |
| `mypy --strict` | ✅ limpo (52 arquivos de `app`, mais 9 da suíte) |

Dois defeitos encontrados pela própria suíte e registrados no placar, não corrigidos
nesta sessão (fora do escopo da S11) — **ambos corrigidos na S25**, D-38 e D-39:

1. **`emergencia_placeholders`** — a resposta de emergência sai com `{address}` e
   `{phone}` literais. `AgentEngine._input_guardrail` não passa `placeholders` para
   `InputGuardrail.check`. Acontece na mensagem que manda a pessoa ir à clínica com o
   animal ferido.
2. **`pedido_humano_com_aviso`** — quem pede para falar com uma pessoa não recebe resposta
   nenhuma: o gatilho `pedido_humano` escala sem `reply`, e a mensagem genérica só entra
   em gatilho que não escala. O handoff abre e a equipe é avisada; o cliente fica no vácuo.

---

Estado ao fim da S25, contra Postgres 16 e Redis reais:

| Item | Estado |
|---|---|
| Migrations `0001`–`0005` | ✅ aplicadas (`0005 (head)`) — nenhuma nova: a correção é de config e de código |
| DEF-01 (placeholder não resolvido) | ✅ corrigido — `app/domain/templates.py`, `identity.address`/`identity.phone`, `placeholders` no motor |
| DEF-02 (escalada muda) | ✅ corrigido — aviso padrão em código, redação por `messages.handoff_notice` |
| Endereço e telefone no bloco `[IDENTIDADE]` (D-40) | ✅ com cenário `endereco_da_unidade` |
| `emergencia_placeholders` e `pedido_humano_com_aviso` | ✅ `pass` no placar (eram os dois `known_failure`) |
| `emergencia` e `pedido_humano` | ✅ continuam passando, sem alteração nos cenários |
| `docs/DEFEITOS.md` | ✅ sem defeito aberto |
| Total: 531 testes (37 novos), nenhum pulado | ✅ verde |
| `ruff check` + `ruff format --check` | ✅ limpo |
| `mypy --strict` | ✅ limpo (53 arquivos de `app`) |


---

Estado ao fim da S12:

| Item | Estado |
|---|---|
| `CalendarProvider` (Protocol da 14.3) + `GoogleCalendarProvider` | ✅ `app/calendar/base.py`, `app/calendar/google.py` |
| Aceite com `respx`: `freebusy`, insert idempotente (mesmo `iCalUID` = 1 evento), `410 Gone` no `syncToken`, `403 rateLimitExceeded` | ✅ 43 testes em `tests/unit/test_google_calendar.py` |
| Mutação: sem tratar `409`/`410`, 3 testes caem | ✅ verificado à mão |
| `push_appointment_event` (`pending_sync`) — lógica de estado | ✅ 4 testes unitários, sessão falsa |
| `push_appointment_event` — `UPDATE` contra Postgres real | ✅ 2 testes em `tests/integration/test_calendar_sync_state.py` |
| `ruff check` | ✅ limpo |
| `mypy app` | ✅ limpo (57 arquivos) |
| Total: 580 testes, nenhum pulado, contra Postgres 16 e Redis reais | ✅ verde |

---

Estado ao fim da S13, contra Postgres 16 e Redis reais:

| Item | Estado |
|---|---|
| Migrations `0001`–`0005` | ✅ aplicadas — nenhuma nova: o motor só lê |
| `app/scheduling/rules.py` (regras da 13.3) | ✅ 27 testes em `tests/unit/test_scheduling_rules.py` |
| `app/scheduling/availability.py` (algoritmo da 13.2) — bateria da 19.1 | ✅ 62 testes em `tests/unit/test_scheduling_availability.py`: horário de verão (salto e recuo, NY 2026 e SP 2018/2019), virada de dia, jornada partida, feriado e véspera, buffers, recurso escasso, serviço maior que a janela (vazio, e nenhum slot estoura a janela em 18 combinações) |
| `find_availability` — ocupação do banco com buffers, hold vencido, cancelado, recurso entre profissionais, calendário sem resposta | ✅ 7 testes em `tests/integration/test_availability.py` |
| Total: 676 testes, nenhum pulado | ✅ verde |
| `ruff check` + `ruff format --check` | ✅ limpo |
| `mypy --strict` | ✅ limpo (59 arquivos de `app`) |

---

Estado ao fim da S14, contra Postgres 16 e Redis reais:

| Item | Estado |
|---|---|
| Migrations `0001`–`0005` | ✅ aplicadas — nenhuma nova: `holds`, `appointments`, `appt_no_overlap` e `uq_appointment_idempotency` já vinham da S02 |
| `app/scheduling/booking.py` — `hold_slot`, `confirm_appointment`, `expire_holds` | ✅ 10 testes unitários + 16 de integração em `tests/integration/test_booking.py` |
| Camada 3: `appt_no_overlap` traduzida para `slot_taken` | ✅ escrita concorrente sem lock, sem commit, segurando o `INSERT` da confirmação |
| Aceite: 20 pedidos simultâneos ao mesmo slot → 1 agendamento (testcontainers) | ✅ `tests/integration/test_booking_concurrency.py`, 3 disputas (fluxo completo, holds furando a camada 1, mesmo hold 20 vezes) |
| Aceite: confirmar de novo o mesmo hold → mesmo `appointment_id`, sem linha nova | ✅ em sequência e com 20 confirmações simultâneas |
| Mutação: sem advisory lock no hold, 14 holds para o mesmo slot; sem a tradução, a `ExclusionViolationError` vaza | ✅ verificado, os testes caem |
| `expire_holds_job` no cron (a cada minuto) | ✅ |
| Total: 705 testes, nenhum pulado | ✅ verde |
| `ruff check` + `ruff format --check` | ✅ limpo |
| `mypy --strict` | ✅ limpo (60 arquivos de `app`) |

---

Estado ao fim da S15, contra Postgres 16 e Redis reais:

| Item | Estado |
|---|---|
| Migrations `0001`–`0005` | ✅ aplicadas — nenhuma nova: o fluxo usa `holds`, `appointments`, `subjects` e `contacts` como já estavam |
| `slot_token` (`app/scheduling/slot_token.py`): adulterado, inventado, de outra conversa, de outro tenant, vencido, malformado | ✅ 37 testes unitários |
| Aceite: `slot_token` adulterado é rejeitado — e **antes do banco** (zero linhas em `holds`) | ✅ `tests/integration/test_tools_scheduling.py` |
| Aceite: `confirm_appointment` com hold expirado → `{"status": "hold_expired"}` com `suggested_action` | ✅ idem, sem agendamento criado |
| Aceite: o modelo não agenda horário que não veio de `check_availability` | ✅ pelo **motor inteiro**: modelo roteirizado tenta horário escrito à mão, token inventado e hold inexistente — três erros, zero linhas, estágio não avança |
| Fluxo completo pelo motor (check → hold → confirm → resposta) com o guardrail de saída ligado | ✅ resposta aprovada sem regeneração, estágio `booked`; horário fora da oferta é barrado e regenerado |
| 5 tools (`check_availability`, `hold_slot`, `confirm_appointment`, `find_appointments`, `save_contact_info`) + resolver de calendário | ✅ 56 unitários + 57 de integração + 1 no `test_booking.py` |
| Mutação: 9 quebras de propósito (assinatura, chave sem contexto, validade, dono do hold, falha como sucesso, confirmação dobrada, troca sem savepoint, `ends_at` e `expires_at` ISO) | ✅ as 9 derrubam teste |
| Total: 859 testes (154 novos), nenhum pulado | ✅ verde |
| `ruff check` + `ruff format --check` | ✅ limpo |
| `mypy --strict` | ✅ limpo (68 arquivos de `app`) |

**Não verificado, e por quê:**

- **Google Calendar real.** O espelho foi testado contra um calendário falso com estado (o
  contrato de `iCalUID` e `sync_status`). A S12 cobre o cliente HTTP com `respx`; a
  composição das duas pontas com a API do Google nunca rodou.
- **Modelo real.** O motor foi exercitado com modelo roteirizado. Que o modelo de verdade
  copie o token sem erro e chame as tools na ordem certa só aparece com
  `CONVERSATIONAL_LIVE=1` — e os cenários dessa suíte ainda usam **stubs** de
  `check_availability` e `confirm_appointment` (D-36). Trocá-los pelo handler real muda a
  contagem de asserções do placar (D-34) e ficou para decisão própria.
- **Link de videochamada em serviço `online`** (13.3; a D-47 o atribuía à S15). Não
  implementado: exige `conferenceData` no `EventDraft` e no `GoogleCalendarProvider`
  (S12). Hoje o evento de serviço online sai sem link.
- **Intake completo exigido em código no `confirm_appointment`.** Não implementado: as
  chaves do intake (`service`, `contact_name`...) têm semântica do tenant, e exigi-las no
  código pediria um vínculo entre chave e fato (serviço do hold, nome do contato) que não
  está desenhado. Hoje a garantia é a do prompt, que recebe `missing` calculado em código.
- **Calendário de recurso** (D-58): recurso com `calendar_id` fica fora da oferta.
