"""Runner da suite conversacional (secao 19.3).

O que este arquivo executa
--------------------------
Um cenario YAML vira uma conversa: para cada turno, a fala do cliente entra no historico,
o motor roda com os guardrails e as tools de verdade, a resposta volta e as assercoes do
turno sao conferidas. O ciclo repete o que `app.agent.runner` faz em producao — passo 0
(silencio por handoff), turno, e abertura de handoff quando o turno escala — com estado
em memoria no lugar do Postgres.

Por que nao reusar `AgentTurnHandler`
--------------------------------------
Ele carrega e grava estado por SQL. Amarrar a suite conversacional ao banco tornaria o
requisito "roda offline" impossivel e faria cada mudanca de prompt depender de docker.
O que a suite precisa do handler e a **ordem** das etapas, nao a persistencia — e a ordem
esta replicada aqui, em `ConversationDriver.run_turn`, curta o bastante para ser lida
lado a lado com o original.

Assercoes
---------
As cinco da 19.3 (`handoff_opened`, `handoff_reason`, `response_contains_any`,
`no_tool_called`, `max_turns_to_escalate`) mais seis que os cenarios de hoje exigem e
que seguem o mesmo criterio: tudo deterministico e conferivel sem julgamento. Tom e a
unica coisa que precisa de julgamento, e ela vai para o juiz LLM (`judge.py`), que e
opcional e nunca entra no placar.

Texto e comparado com `app.agent.temporal.fold` — sem acento e sem caixa. A resposta pode
vir de um YAML de tenant escrito em ASCII e a assercao ser escrita como se escreve em
portugues; exigir casamento byte a byte transformaria acento em falha de cenario.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.engine import AgentEngine, ConversationState, TurnOutcome, TurnRequest
from app.agent.guardrails import OutputGuardrail
from app.agent.handoff import triggered_by_for
from app.agent.llm import LLMProvider
from app.agent.memory import StoredMessage
from app.agent.prompt import KnowledgeSnippet, PersonContext, ServiceSummary
from app.agent.temporal import fold
from app.agent.tools.base import (
    ToolContext,
    ToolHandler,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from app.agent.tools.escalate_to_human import escalate_to_human_tool
from app.agent.tools.list_services import list_services_tool
from app.agent.tools.search_knowledge import search_knowledge_tool
from app.core.time import tz_of
from app.domain.tenant_config import TenantConfig, load_tenant_config
from app.knowledge.embeddings import HashingEmbeddingProvider
from tests.conversational.fakes import (
    FakeCalendar,
    FakeChannel,
    InMemoryHandoffs,
    RecordingInputGuardrail,
    RecordingProvider,
    ScriptedModel,
    ScriptedStep,
)
from tests.conversational.judge import ToneJudge

__all__ = [
    "Scenario",
    "ScenarioResult",
    "TurnResult",
    "load_scenario",
    "run_scenario",
    "scenario_paths",
]

SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"
TENANTS_DIR = Path(__file__).resolve().parents[2] / "config" / "tenants"

#: Relogio congelado padrao: terca, 08/09/2026, 10h em Sao Paulo — dia util, dentro do
#: horario de funcionamento do tenant de exemplo. Cenario que precise de outro instante
#: declara `now` no YAML; nenhum usa `datetime.now()`, porque um cenario que muda de
#: resultado conforme o dia da semana nao serve de placar.
DEFAULT_NOW = "2026-09-08T10:00:00"

#: O que o modelo diz quando o roteiro do turno acaba. Neutro de proposito: sem preco,
#: sem data e sem hora, para nao disparar o guardrail de saida por acidente.
DEFAULT_FALLBACK = "Vou confirmar isso com a equipe e ja te retorno."

#: Numero do contato ficticio da conversa. Nao e o `escalation.notify` do tenant.
CONTACT_NUMBER = "+5511900000000"

#: Bloco delimitado de `app.agent.prompt.wrap_user_content`.
_DADO_RE = re.compile(r"<dado[^>]*>(.*?)</dado>", re.DOTALL)


# =============================== cenario (dados) ===============================


class _Model(BaseModel):
    """Base com `extra='forbid'`.

    Assercao com nome errado tem de quebrar o cenario, nao passar despercebida: uma
    suite que ignora `respose_contains_any` em silencio da o verde sem ter conferido
    nada — o pior defeito possivel num arquivo de teste.
    """

    model_config = ConfigDict(extra="forbid")


class ServiceFixture(_Model):
    id: str = ""
    name: str
    duration_minutes: int = 30
    price_line: str = ""
    modality: str = "in_person"
    description: str = ""


class KnowledgeFixture(_Model):
    title: str
    content: str
    score: float = 0.9


class StubTool(_Model):
    """Tool extra do cenario: schema declarado no YAML, resultado fixo.

    Existe para os nomes que a 19.3 ja cita (`check_availability`,
    `confirm_appointment`) e que so ganham implementacao na S13/S15. Declarar o schema
    no dado, e nao em Python, evita fixar aqui uma interface que ainda nao foi desenhada
    — quando ela existir, o cenario troca o stub pela tool de verdade.
    """

    name: str
    description: str = "tool declarada pelo cenario"
    kind: Literal["read", "write"] = "read"
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    result: dict[str, Any] = Field(default_factory=dict)
    #: Slots relativos (`+1 14:30`) resolvidos pelo `FakeCalendar` e entregues como
    #: `{"slots": [...]}`. Data absoluta apodrece; ver `FakeCalendar`.
    slots: list[str] = Field(default_factory=list)
    error: bool = False


class AgentScript(_Model):
    """Um passo do roteiro do modelo. Ou `say`, ou `call`+`arguments`."""

    say: str | None = None
    call: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exactly_one(self) -> AgentScript:
        if (self.say is None) == (self.call is None):
            raise ValueError("cada passo do roteiro tem `say` ou `call`, nunca os dois")
        return self

    def to_step(self) -> ScriptedStep:
        return ScriptedStep(say=self.say, tool=self.call, arguments=self.arguments)


class Expect(_Model):
    """As assercoes de um turno. Campo ausente nao e conferido."""

    handoff_opened: bool | None = None
    handoff_reason: str | None = None
    response_contains_any: list[str] = Field(default_factory=list)
    response_excludes_any: list[str] = Field(default_factory=list)
    no_tool_called: list[str] = Field(default_factory=list)
    tools_called: list[str] = Field(default_factory=list)
    max_turns_to_escalate: int | None = None
    stage: str | None = None
    injection_flags: list[str] = Field(default_factory=list)
    #: Invariante 7: a fala do cliente chegou ao modelo dentro do bloco `<dado>`.
    prompt_isolates_user_content: bool | None = None
    #: RF-28: o agente ficou calado porque ha handoff aberto dentro do prazo.
    silenced: bool | None = None
    #: Criterio de tom para o juiz LLM. Ignorado (e registrado) sem juiz instalado.
    tone: str | None = None

    def count(self) -> int:
        """Quantas assercoes este turno declara. E o numero que o placar versiona."""
        declared = 0
        for name, value in self.model_dump().items():
            if value is None or value == []:
                continue
            declared += len(value) if isinstance(value, list) and name != "tone" else 1
        return declared


class TurnSpec(_Model):
    user: str
    agent: list[AgentScript] = Field(default_factory=list)
    expect: Expect = Field(default_factory=Expect)


class Scenario(_Model):
    scenario: str
    tenant: str = "clinica-exemplo"
    description: str = ""
    now: datetime = datetime.fromisoformat(DEFAULT_NOW)
    services: list[ServiceFixture] = Field(default_factory=list)
    knowledge: list[KnowledgeFixture] = Field(default_factory=list)
    tools: list[StubTool] = Field(default_factory=list)
    calendar_busy: list[str] = Field(default_factory=list)
    collected: dict[str, Any] = Field(default_factory=dict)
    #: Ligar a extracao estruturada (11.7) com este resultado fixo. Ausente = desligada,
    #: para o cenario nao gastar um passo de roteiro numa chamada que ele nao observa.
    extraction: dict[str, Any] | None = None
    fallback: str = DEFAULT_FALLBACK
    turns: list[TurnSpec]

    def assertion_count(self) -> int:
        return sum(turn.expect.count() for turn in self.turns)


def load_scenario(path: Path) -> Scenario:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Scenario.model_validate(data)


def scenario_paths() -> list[Path]:
    return sorted(SCENARIOS_DIR.glob("*.yaml"))


# ============================== resultado (saida) ==============================


@dataclass(frozen=True, slots=True)
class TurnResult:
    index: int
    user: str
    reply: str
    messages: tuple[str, ...]
    tools: tuple[str, ...]
    escalated: bool
    reason: str | None
    stage: str
    injection_flags: tuple[str, ...]
    #: Categoria estruturada de `escalate_to_human`, quando o modelo chamou a tool.
    #: `escalation_reason` guarda o texto livre do motivo; a categoria e o rotulo
    #: fechado, e e por ele que os cenarios afirmam `handoff_reason`.
    categories: tuple[str, ...]
    silenced: bool
    llm_calls: int
    failures: tuple[str, ...] = ()
    tone: str | None = None


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    name: str
    turns: tuple[TurnResult, ...]
    failures: tuple[str, ...]
    assertions: int
    handoffs: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def transcript(self) -> str:
        lines: list[str] = []
        for turn in self.turns:
            lines.append(f"cliente: {turn.user}")
            lines.append(f"agente: {turn.reply or '(silencio)'}")
        return "\n".join(lines)

    def report(self) -> str:
        blocks = [f"cenario {self.name}"]
        for turn in self.turns:
            blocks.append(
                f"  turno {turn.index}\n"
                f"    cliente: {turn.user}\n"
                f"    agente: {turn.reply or '(sem resposta)'}\n"
                f"    tools: {list(turn.tools)} | escalou: {turn.escalated}"
                f" | motivo: {turn.reason} | stage: {turn.stage}"
            )
        for failure in self.failures:
            blocks.append(f"  FALHA: {failure}")
        return "\n".join(blocks)


# ================================== execucao ==================================


def _stub_handler(stub: StubTool, calendar: FakeCalendar) -> ToolHandler:
    async def handler(ctx: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        if stub.error:
            return ToolResult(content={"error": "falha_da_ferramenta"}, is_error=True)
        content = dict(stub.result)
        if stub.slots:
            content["slots"] = calendar.free_slots(stub.slots)
        return ToolResult(content=content)

    return handler


def _catalog_handler(services: Sequence[ServiceFixture]) -> ToolHandler:
    payload: list[dict[str, Any]] = [
        {
            "id": service.id or service.name.lower().replace(" ", "-"),
            "name": service.name,
            "duration_minutes": service.duration_minutes,
            "price_line": service.price_line,
            "modality": service.modality,
            "description": service.description,
        }
        for service in services
    ]

    async def handler(ctx: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        query = fold(str(arguments.get("query") or ""))
        matched = [item for item in payload if not query or query in fold(str(item["name"]))]
        return ToolResult(content={"services": matched})

    return handler


def _knowledge_handler(snippets: Sequence[KnowledgeFixture]) -> ToolHandler:
    async def handler(ctx: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        query = fold(str(arguments.get("query") or ""))
        results = [
            {"title": snippet.title, "content": snippet.content, "score": snippet.score}
            for snippet in snippets
            if any(word in fold(snippet.title + " " + snippet.content) for word in query.split())
        ]
        return ToolResult(content={"results": results})

    return handler


def build_registry(scenario: Scenario, calendar: FakeCalendar) -> ToolRegistry:
    """Tools do cenario: as de verdade com o handler trocado, mais os stubs do YAML.

    O schema que o modelo ve continua sendo o de producao — nome, descricao e
    `input_schema` saem da propria tool. Trocar so o handler mantem o contrato honesto:
    no modo ao vivo, o modelo decide a partir da mesma descricao que vai para o cliente.
    """
    specs: list[ToolSpec] = [
        replace(list_services_tool(), handler=_catalog_handler(scenario.services)),
        replace(
            search_knowledge_tool(HashingEmbeddingProvider()),
            handler=_knowledge_handler(scenario.knowledge),
        ),
        escalate_to_human_tool(),
    ]
    for stub in scenario.tools:
        specs.append(
            ToolSpec(
                name=stub.name,
                description=stub.description,
                input_schema=stub.input_schema,
                handler=_stub_handler(stub, calendar),
                kind=stub.kind,
            )
        )
    return ToolRegistry(tuple(specs))


@dataclass(slots=True)
class ConversationDriver:
    """Roda a conversa inteira, na mesma ordem de `app.agent.runner.AgentTurnHandler`."""

    scenario: Scenario
    config: TenantConfig
    engine: AgentEngine
    provider: RecordingProvider
    model: ScriptedModel | None
    guardrail: RecordingInputGuardrail
    handoffs: InMemoryHandoffs
    channel: FakeChannel
    tenant_id: uuid.UUID = field(default_factory=uuid.uuid4)
    conversation_id: uuid.UUID = field(default_factory=uuid.uuid4)
    contact_id: uuid.UUID = field(default_factory=uuid.uuid4)
    history: list[StoredMessage] = field(default_factory=list)
    collected: Mapping[str, Any] = field(default_factory=dict)
    stage: str = "greeting"
    cost: Decimal = Decimal(0)
    message_count: int = 0
    escalated_at: int | None = None

    async def run_turn(self, index: int, turn: TurnSpec) -> TurnResult:
        now = self._now(index)
        self.history.append(
            StoredMessage(direction="inbound", author="contact", content=turn.user, created_at=now)
        )
        self.message_count += 1

        # Passo 0 (RF-28): handoff aberto e dentro do prazo, o agente nao responde.
        if self.handoffs.silenced(now):
            return TurnResult(
                index=index,
                user=turn.user,
                reply="",
                messages=(),
                tools=(),
                escalated=False,
                reason=None,
                stage=self.stage,
                injection_flags=(),
                categories=(),
                silenced=True,
                llm_calls=0,
            )

        if self.model is not None:
            self.model.load([step.to_step() for step in turn.agent])
        before = len(self.provider.prompts)
        decisions = len(self.guardrail.decisions)

        outcome = await self.engine.run_turn(
            TurnRequest(
                tenant_id=self.tenant_id,
                conversation_id=self.conversation_id,
                contact_id=self.contact_id,
                channel="whatsapp",
                config=self.config,
                now=now,
                state=ConversationState(
                    history=tuple(self.history),
                    collected=self.collected,
                    stage=self.stage,
                    message_count=self.message_count,
                    cost_so_far_usd=self.cost,
                ),
                services=self._services(),
                knowledge=self._knowledge(),
                person=PersonContext(contact_name=self._contact_name()),
            )
        )
        self._absorb(outcome, now)

        if outcome.escalate:
            self.escalated_at = self.escalated_at or index
            self.handoffs.open(
                conversation_id=self.conversation_id,
                reason=outcome.escalation_reason or "sem_motivo_informado",
                triggered_by=triggered_by_for(outcome.escalation_reason),
                now=now,
                silence_minutes=self.config.escalation.handoff_silence_minutes,
            )
        # O canal falso recebe o que o cliente receberia: uma mensagem por parte, ja
        # quebrada pelo `max_message_chars` do tenant (11.5).
        for text in outcome.messages:
            await self.channel.send_text(to=CONTACT_NUMBER, text=text)

        flags = tuple(
            flag
            for decision in self.guardrail.decisions[decisions:]
            for flag in decision.injection_flags
        )
        return TurnResult(
            index=index,
            user=turn.user,
            reply=outcome.reply,
            messages=outcome.messages,
            tools=outcome.used_tools,
            escalated=outcome.escalate,
            reason=outcome.escalation_reason,
            stage=outcome.stage,
            injection_flags=flags,
            categories=_categories(outcome),
            silenced=False,
            llm_calls=len(self.provider.prompts) - before,
        )

    def _absorb(self, outcome: TurnOutcome, now: datetime) -> None:
        """Grava o turno no estado em memoria (o que `persist_turn` faz no banco)."""
        for offset, text in enumerate(outcome.messages, start=1):
            self.history.append(
                StoredMessage(
                    direction="outbound",
                    author="agent",
                    content=text,
                    created_at=now + timedelta(seconds=offset),
                )
            )
        self.message_count += len(outcome.messages)
        self.collected = dict(outcome.collected)
        self.stage = outcome.stage
        self.cost += outcome.cost_usd

    def _contact_name(self) -> str | None:
        nome = str(self.collected.get("contact_name") or "").strip()
        return nome or None

    def _now(self, index: int) -> datetime:
        """Um minuto por turno, no fuso do tenant. Ordem do historico depende disso."""
        base = self.scenario.now
        if base.tzinfo is None:
            base = base.replace(tzinfo=tz_of(self.config.business_hours.timezone))
        return base + timedelta(minutes=index - 1)

    def _services(self) -> tuple[ServiceSummary, ...]:
        return tuple(
            ServiceSummary(
                id=service.id or service.name.lower().replace(" ", "-"),
                name=service.name,
                duration_minutes=service.duration_minutes,
                price_line=service.price_line,
                modality=service.modality,
                description=service.description,
            )
            for service in self.scenario.services
        )

    def _knowledge(self) -> tuple[KnowledgeSnippet, ...]:
        return tuple(
            KnowledgeSnippet(title=s.title, content=s.content, score=s.score)
            for s in self.scenario.knowledge
        )


def build_driver(
    scenario: Scenario,
    *,
    provider_inner: LLMProvider | None = None,
) -> ConversationDriver:
    """Monta o agente do cenario. Sem `provider_inner`, usa o roteiro do YAML."""
    config = load_tenant_config(TENANTS_DIR / f"{scenario.tenant}.yaml")
    now = scenario.now
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz_of(config.business_hours.timezone))
    calendar = FakeCalendar(
        now=now,
        timezone=config.business_hours.timezone,
        busy=tuple(scenario.calendar_busy),
    )
    model = None
    if provider_inner is None:
        model = ScriptedModel(fallback=scenario.fallback, extraction=scenario.extraction or {})
        provider_inner = model
    provider = RecordingProvider(inner=provider_inner)
    guardrail = RecordingInputGuardrail()
    engine = AgentEngine(
        provider=provider,
        tools=build_registry(scenario, calendar),
        extract=scenario.extraction is not None,
        input_guardrail=guardrail,
        output_guardrail=OutputGuardrail(),
    )
    return ConversationDriver(
        scenario=scenario,
        collected=dict(scenario.collected),
        config=config,
        engine=engine,
        provider=provider,
        model=model,
        guardrail=guardrail,
        handoffs=InMemoryHandoffs(),
        channel=FakeChannel(),
    )


# ================================== assercoes ==================================


def _categories(outcome: TurnOutcome) -> tuple[str, ...]:
    return tuple(
        str(call.result.get("category"))
        for call in outcome.tool_calls
        if call.ok and call.name == "escalate_to_human" and call.result.get("category")
    )


def _matches_reason(expected: str, result: TurnResult, handoffs: InMemoryHandoffs) -> bool:
    """Motivo do handoff, aceito em qualquer das formas com que o sistema o escreve.

    O motivo nasce prefixado pela origem (`trigger:emergencia`, `tool:...`) e a 19.3
    escreve so o rotulo (`emergencia`). Aceitar as duas formas evita que a assercao passe
    a testar o prefixo em vez do motivo.
    """
    record = handoffs.current
    candidates = {result.reason or "", record.reason if record else "", *result.categories}
    for raw in tuple(candidates):
        if ":" in raw:
            candidates.add(raw.split(":", 1)[1])
    return any(fold(expected) == fold(candidate) for candidate in candidates if candidate)


def _check_turn(
    turn: TurnSpec, result: TurnResult, driver: ConversationDriver, prompts_before: int
) -> list[str]:
    expect = turn.expect
    failures: list[str] = []
    reply = fold(result.reply)

    if expect.silenced is not None and result.silenced != expect.silenced:
        failures.append(f"silenced: esperado {expect.silenced}, obtido {result.silenced}")
    if expect.handoff_opened is not None:
        opened = bool(driver.handoffs.open_records)
        if opened != expect.handoff_opened:
            failures.append(f"handoff_opened: esperado {expect.handoff_opened}, obtido {opened}")
    if expect.handoff_reason is not None and not _matches_reason(
        expect.handoff_reason, result, driver.handoffs
    ):
        failures.append(
            f"handoff_reason: esperado {expect.handoff_reason!r}, obtido {result.reason!r}"
        )
    if expect.response_contains_any and not any(
        fold(needle) in reply for needle in expect.response_contains_any
    ):
        failures.append(
            f"response_contains_any {expect.response_contains_any}: resposta {result.reply!r}"
        )
    for needle in expect.response_excludes_any:
        if fold(needle) in reply:
            failures.append(f"response_excludes_any {needle!r}: resposta {result.reply!r}")
    for name in expect.no_tool_called:
        if name in result.tools:
            failures.append(f"no_tool_called: {name} foi chamada")
    for name in expect.tools_called:
        if name not in result.tools:
            failures.append(
                f"tools_called: {name} nao foi chamada (chamadas: {list(result.tools)})"
            )
    if expect.max_turns_to_escalate is not None:
        at = driver.escalated_at
        if at is None or at > expect.max_turns_to_escalate:
            failures.append(
                f"max_turns_to_escalate {expect.max_turns_to_escalate}: escalou em {at}"
            )
    if expect.stage is not None and result.stage != expect.stage:
        failures.append(f"stage: esperado {expect.stage}, obtido {result.stage}")
    for flag in expect.injection_flags:
        if flag not in result.injection_flags:
            obtidas = list(result.injection_flags)
            failures.append(f"injection_flags: {flag} nao foi detectada (obtidas: {obtidas})")
    if expect.prompt_isolates_user_content:
        failures.extend(_check_isolation(turn, driver, prompts_before))
    return failures


def _collapse(text: str) -> str:
    return " ".join(fold(text).split())


def _delimited(text: str) -> str:
    """So o que esta dentro dos blocos `<dado>`, concatenado."""
    return " ".join(match.group(1) for match in _DADO_RE.finditer(text))


def _check_isolation(turn: TurnSpec, driver: ConversationDriver, prompts_before: int) -> list[str]:
    """Invariante 7: a fala do cliente so aparece dentro do bloco `<dado>`.

    Confere as duas metades da invariante — que o texto nao vazou para o system prompt
    (onde ele viraria instrucao) e que, na janela, ele esta dentro do bloco delimitado.
    """
    prompts = driver.provider.prompts[prompts_before:]
    if not prompts:
        return ["prompt_isolates_user_content: o modelo nao foi chamado neste turno"]

    alvo = _collapse(turn.user)
    for prompt in prompts:
        if alvo and alvo in _collapse(prompt.system_text):
            return ["prompt_isolates_user_content: a fala do cliente vazou para o system prompt"]
        janela = prompt.user_text
        if alvo and alvo in _collapse(janela) and alvo not in _collapse(_delimited(janela)):
            return ["prompt_isolates_user_content: a fala do cliente nao veio delimitada"]
    return []


async def run_scenario(scenario: Scenario, *, judge: ToneJudge | None = None) -> ScenarioResult:
    """Executa o cenario e devolve o resultado com as falhas de cada turno.

    Nao levanta: quem decide se a falha vira erro de teste e `test_cenarios.py` (por
    cenario) ou `test_placar.py` (contra o placar versionado).
    """
    driver = build_driver(scenario)
    results: list[TurnResult] = []
    failures: list[str] = []

    for index, turn in enumerate(scenario.turns, start=1):
        prompts_before = len(driver.provider.prompts)
        result = await driver.run_turn(index, turn)
        turn_failures = _check_turn(turn, result, driver, prompts_before)
        results.append(replace(result, failures=tuple(turn_failures), tone=turn.expect.tone))
        failures.extend(f"turno {index}: {failure}" for failure in turn_failures)

    if judge is not None:
        for turn, result in zip(scenario.turns, results, strict=True):
            if not turn.expect.tone or not result.reply:
                continue
            verdict = await judge.evaluate(criteria=turn.expect.tone, reply=result.reply)
            if not verdict.ok:
                failures.append(f"turno {result.index}: tone — {verdict.reason}")

    return ScenarioResult(
        name=scenario.scenario,
        turns=tuple(results),
        failures=tuple(failures),
        assertions=scenario.assertion_count(),
        handoffs=tuple(record.reason for record in driver.handoffs.open_records),
    )
