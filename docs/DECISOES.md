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
nesta sessão (fora do escopo da S11):

1. **`emergencia_placeholders`** — a resposta de emergência sai com `{address}` e
   `{phone}` literais. `AgentEngine._input_guardrail` não passa `placeholders` para
   `InputGuardrail.check`. Acontece na mensagem que manda a pessoa ir à clínica com o
   animal ferido.
2. **`pedido_humano_com_aviso`** — quem pede para falar com uma pessoa não recebe resposta
   nenhuma: o gatilho `pedido_humano` escala sem `reply`, e a mensagem genérica só entra
   em gatilho que não escala. O handoff abre e a equipe é avisada; o cliente fica no vácuo.
