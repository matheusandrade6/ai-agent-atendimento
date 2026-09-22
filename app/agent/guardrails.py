"""Guardrails de entrada e de saida, e o disjuntor da conversa (secao 11.5).

Onde este arquivo se encaixa
----------------------------
No loop da 11.1 ele e o passo 3 (antes do LLM) e o passo 8 (depois do LLM, antes de
enviar). O motor (`app.agent.engine`) chama os dois; nada aqui toca banco, fila ou rede.

O que este arquivo esta defendendo
----------------------------------
A tabela de riscos da secao 21 tem uma linha de impacto **alto** que depende
inteiramente daqui: *alucinacao de horario ou preco*. Um agente que promete "quarta as
14h" sem ter chamado `check_availability` cria um cliente que aparece na clinica sem ter
horario. O prompt pede que isso nao aconteca; o prompt nao e garantia. A garantia e a
conferencia mecanica que acontece aqui, depois que o modelo ja escreveu.

O criterio que organiza as decisoes
-----------------------------------
Os dois erros possiveis nao tem o mesmo preco:

- **Deixar passar** um horario inventado: a pessoa vai ate o lugar a toa. Irreversivel.
- **Descartar** uma resposta boa: custa uma chamada de LLM a mais; na segunda falha, um
  humano assume. Recuperavel.

Entao, em toda duvida, este arquivo erra para o lado de descartar. As unicas excecoes
sao as que existem para nao transformar conversa normal em handoff — estao marcadas uma
a uma no codigo, porque cada uma delas e uma folga deliberada na defesa.

Conteudo do usuario continua sendo dado
---------------------------------------
O detector de prompt injection **nao bloqueia** a conversa (ver `detect_injection`). A
defesa contra injecao e estrutural — texto de terceiro entra delimitado
(`app.agent.prompt.wrap_user_content`) e o prompt diz que ali dentro e dado. O detector
existe para registrar e para alimentar metrica, nao para virar um filtro que qualquer
cliente irritado dispara sem querer.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal, Protocol

from app.agent.temporal import (
    Evidence,
    TemporalClaim,
    find_mentions,
    fold,
    iter_strings,
    pair_claims,
)
from app.core.telemetry import get_logger
from app.domain.conditions import evaluate_condition
from app.domain.tenant_config import EscalationTrigger, Limits, TenantConfig

log = get_logger(__name__)

__all__ = [
    "InputDecision",
    "InputGuardrail",
    "OutputContext",
    "OutputDecision",
    "OutputGuardrail",
    "TurnRateLimiter",
    "Violation",
    "business_hour_bounds",
    "circuit_breaker",
    "detect_injection",
    "extract_prices",
    "match_trigger",
    "redact_pii",
    "regeneration_hint",
    "split_message",
]


# ================================= entrada (passo 3) =================================


class TurnRateLimiter(Protocol):
    """Contador anti-flood por contato.

    `app.workers.ratelimit.ContactRateLimiter` satisfaz este protocolo. Quem roda atras
    do worker de entrada ja foi contado la, no ponto mais barato possivel — por isso o
    campo e opcional aqui (docs/DECISOES.md, D-28).
    """

    async def allow(self, *, tenant_id: uuid.UUID, contact_id: uuid.UUID) -> bool: ...


InputAction = Literal["allow", "reply", "escalate", "drop"]


@dataclass(frozen=True, slots=True)
class InputDecision:
    """O que fazer com a rajada antes de gastar um token de LLM."""

    action: InputAction = "allow"
    reply: str = ""
    trigger_id: str | None = None
    trigger_action: str | None = None
    reason: str | None = None
    injection_flags: tuple[str, ...] = ()

    @property
    def short_circuits(self) -> bool:
        return self.action != "allow"

    @property
    def escalates(self) -> bool:
        return self.action == "escalate"


# ------------------------------- intencoes embutidas -------------------------------

#: `match_intent` da config aponta para intencoes genericas — nao sao de nenhum cliente,
#: sao da lingua. Ficam em codigo, deterministicas: mandar uma segunda chamada de LLM
#: so para classificar "quero falar com atendente" gastaria mais do que o turno inteiro.
INTENT_PATTERNS: Final[Mapping[str, re.Pattern[str]]] = {
    "ask_for_human": re.compile(
        r"\b(?:falar|conversar)\s+com\s+(?:uma?\s+)?"
        r"(?:humano|pessoa|atendente|alguem|responsavel|gerente|secretaria|recepcao)\b"
        r"|\b(?:quero|queria|posso|gostaria\s+de)\s+(?:falar|conversar)\s+com\s+alguem\b"
        r"|\b(?:me\s+)?(?:transfere|transferir|passa|passar)\s+para\s+"
        r"(?:um[a]?\s+)?(?:humano|atendente|pessoa|alguem|responsavel)\b"
        r"|\b(?:nao|n)\s+quero\s+(?:falar\s+com\s+)?(?:rob[oô]|bot|maquina|ia)\b"
        r"|\batendimento\s+humano\b"
    ),
    "medical_advice": re.compile(
        r"\b(?:e|sera\s+que\s+e|pode\s+ser|acha\s+que\s+e)\s+"
        r"(?:grave|serio|normal|perigoso|urgente)\b"
        r"|\b(?:o\s+que|oq)\s+(?:sera\s+que\s+)?(?:ele|ela|meu|minha|isso)?\s*\w*\s*(?:tem|pode\s+ter)\b"
        r"|\b(?:posso|devo|pode)\s+(?:dar|aplicar|passar|usar|tomar|administrar)\b"
        r"|\b(?:qual|que)\s+(?:remedio|medicamento|dose|dosagem|pomada|antibiotico)\b"
        r"|\bpreciso\s+(?:mesmo\s+)?(?:levar|ir)\s+(?:ao?|na?|no)\s+(?:veterinario|medico|clinica)\b"
        r"|\b(?:diagnostic|sintoma|receita|receitar|prescrev)\w*\b"
    ),
}


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    """Palavra-chave da config vira regex com fronteira e espaco flexivel.

    Fronteira importa: sem ela, o gatilho `urgente` dispararia dentro de
    `nao e urgente`. Bem — essa dispara mesmo, e o falso positivo aqui custa um
    handoff, que e o lado barato do erro num gatilho de emergencia.
    """
    parts = [re.escape(part) for part in keyword.strip().split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(parts) + r"(?!\w)")


#: Ordem de gravidade. Um gatilho de emergencia declarado **depois** de um gatilho de
#: recusa nao pode perder para ele so por causa da ordem no YAML: "meu cachorro foi
#: atropelado, o que eu faco?" casa com os dois, e a resposta certa e a emergencia.
_ACTION_SEVERITY: Final[Mapping[str, int]] = {
    "escalate_immediately": 0,
    "escalate": 1,
    "refuse_and_offer_appointment": 2,
    "refuse": 3,
}


def match_trigger(
    triggers: Sequence[EscalationTrigger],
    text: str,
    *,
    signals: Mapping[str, Any] | None = None,
) -> EscalationTrigger | None:
    """Primeiro gatilho que casa, pela gravidade da acao e depois pela ordem do YAML.

    Casa por palavra-chave, por regex (`match_regex`), por intencao embutida
    (`match_intent`) ou por condicao sobre sinais do turno (`condition`). A condicao usa
    o mesmo interpretador fechado do intake (`app.domain.conditions`) — nunca `eval`.
    """
    folded = fold(text)
    data = dict(signals or {})
    candidates: list[tuple[int, int, EscalationTrigger]] = []

    for order, trigger in enumerate(triggers):
        if _trigger_matches(trigger, folded, data):
            candidates.append((_ACTION_SEVERITY.get(trigger.action, 9), order, trigger))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _trigger_matches(trigger: EscalationTrigger, folded: str, signals: Mapping[str, Any]) -> bool:
    for keyword in trigger.match_keywords:
        if _keyword_pattern(fold(keyword)).search(folded):
            return True
    for expression in trigger.match_regex:
        if re.search(expression, folded, re.IGNORECASE):
            return True
    if trigger.match_intent is not None:
        pattern = INTENT_PATTERNS.get(trigger.match_intent)
        if pattern is not None and pattern.search(folded):
            return True
    if trigger.condition is not None and signals:
        return evaluate_condition(trigger.condition, signals)
    return False


# ------------------------------- prompt injection -------------------------------

#: Formas conhecidas de tentar transformar conteudo em instrucao. A lista nao precisa
#: ser exaustiva — ela alimenta log e metrica, nao um bloqueio (ver o docstring do
#: modulo e `detect_injection`).
_INJECTION_PATTERNS: Final[Mapping[str, re.Pattern[str]]] = {
    "override": re.compile(
        r"\b(?:ignor[ea]|esquec[ae]|desconsider[ea]|apague)\b[^.!?]{0,40}"
        r"\b(?:instruc|regra|comando|orienta|acima|anterior|prompt|contexto)\w*"
        r"|\bignore\s+(?:all\s+)?(?:previous|prior|above)\b"
    ),
    "role_play": re.compile(
        r"\b(?:voce\s+(?:agora\s+)?(?:e|sera|passa\s+a\s+ser)|aja\s+como|finja\s+(?:que|ser)|"
        r"se\s+passe\s+por|assuma\s+o\s+papel|pretend\s+to\s+be|you\s+are\s+now)\b"
    ),
    "reveal_prompt": re.compile(
        r"\b(?:mostre|revele|repita|imprima|qual\s+e|me\s+diga)\b[^.!?]{0,40}"
        r"\b(?:system\s+prompt|prompt\s+de\s+sistema|suas\s+instruc\w*|seu\s+prompt|"
        r"regras\s+internas)\b"
        r"|\b(?:show|reveal|print|repeat)\b[^.!?]{0,30}\b(?:system\s+prompt|instructions)\b"
    ),
    "jailbreak": re.compile(
        r"\b(?:modo\s+(?:desenvolvedor|debug|deus)|developer\s+mode|jailbreak|"
        r"sem\s+(?:nenhuma\s+)?restric\w*|do\s+anything\s+now|dan\s+mode)\b"
    ),
    "fake_delimiter": re.compile(
        r"</?\s*dado\b|\[\s*(?:limites\s+inegociaveis|identidade|seu\s+papel|sistema|system)\s*\]"
        r"|^\s*(?:system|assistant)\s*:",
        re.MULTILINE,
    ),
    "tool_injection": re.compile(
        r"\b(?:chame|execute|rode|invoque|call)\b[^.!?]{0,30}"
        r"\b(?:confirm_appointment|check_availability|hold_slot|escalate_to_human|tool)\b"
        r"|\btenant_id\s*[:=]"
    ),
}


def detect_injection(text: str) -> tuple[str, ...]:
    """Marcas de tentativa de tratar conteudo como instrucao.

    **Nao bloqueia nada de proposito.** A defesa contra injecao e a delimitacao do
    conteudo no prompt (invariante 7), nao este detector. Transformar a deteccao em
    bloqueio criaria um jeito trivial de negar atendimento a qualquer pessoa — bastaria
    ela escrever "ignora o que eu disse antes", que e portugues comum — e trocaria uma
    defesa que funciona por uma que so parece funcionar.
    """
    folded = fold(text)
    return tuple(name for name, pattern in _INJECTION_PATTERNS.items() if pattern.search(folded))


# --------------------------------- guardrail de entrada ---------------------------------


@dataclass(slots=True)
class InputGuardrail:
    """Passo 3 da 11.1: o que acontece antes de o modelo ser chamado."""

    rate_limiter: TurnRateLimiter | None = None

    async def check(
        self,
        *,
        config: TenantConfig,
        text: str,
        tenant_id: uuid.UUID,
        contact_id: uuid.UUID,
        signals: Mapping[str, Any] | None = None,
        placeholders: Mapping[str, str] | None = None,
    ) -> InputDecision:
        if self.rate_limiter is not None:
            allowed = await self.rate_limiter.allow(tenant_id=tenant_id, contact_id=contact_id)
            if not allowed:
                # Silencio e a resposta certa: avisar do limite so alimenta o flood.
                log.warning("guardrail_entrada_rate_limit", contact_id=str(contact_id))
                return InputDecision(action="drop", reason="rate_limit")

        flags = detect_injection(text)
        if flags:
            log.warning("guardrail_entrada_injecao", marcas=list(flags))

        trigger = match_trigger(config.escalation.triggers, text, signals=signals)
        if trigger is None:
            return InputDecision(injection_flags=flags)

        reply = _safe_format(trigger.reply.strip(), placeholders or {})
        escalates = trigger.action in ("escalate", "escalate_immediately")
        if not reply and not escalates:
            # Recusa sem texto configurado cai na mensagem generica do tenant; sem ela,
            # nao ha o que curto-circuitar e o turno segue para o modelo.
            reply = config.messages.out_of_scope.strip()
            if not reply:
                log.warning("guardrail_entrada_recusa_sem_texto", trigger=trigger.id)
                return InputDecision(injection_flags=flags)

        log.info("guardrail_entrada_curto_circuito", trigger=trigger.id, acao=trigger.action)
        return InputDecision(
            action="escalate" if escalates else "reply",
            reply=reply,
            trigger_id=trigger.id,
            trigger_action=trigger.action,
            reason=f"trigger:{trigger.id}",
            injection_flags=flags,
        )


_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def _safe_format(template: str, values: Mapping[str, str]) -> str:
    """`str.format` que nao explode com placeholder desconhecido.

    O texto vem do YAML do cliente e pode citar `{address}` antes de existir um endereco
    para preencher. Um `KeyError` aqui deixaria a pessoa sem resposta num gatilho de
    emergencia — o lugar do projeto onde isso e menos aceitavel.
    """
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)


# ================================== saida (passo 8) ==================================

ViolationKind = Literal[
    "escopo_clinico",
    "slot_sem_evidencia",
    "hora_sem_evidencia",
    "data_sem_evidencia",
    "preco_sem_evidencia",
    "pii_solicitada",
]


@dataclass(frozen=True, slots=True)
class Violation:
    kind: ViolationKind
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


Verdict = Literal["pass", "regenerate", "replace"]


@dataclass(frozen=True, slots=True)
class OutputDecision:
    verdict: Verdict
    messages: tuple[str, ...]
    violations: tuple[Violation, ...] = ()
    redacted: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return "\n\n".join(self.messages)

    @property
    def ok(self) -> bool:
        return self.verdict == "pass"

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(v.kind for v in self.violations)


@dataclass(frozen=True, slots=True)
class OutputContext:
    """Tudo que a conferencia precisa saber sobre o turno que acabou de rodar."""

    config: TenantConfig
    today: date
    evidence: Evidence = field(default_factory=Evidence)
    prices: frozenset[Decimal] = frozenset()


# --------------------------------- escopo clinico ---------------------------------

#: Classificador leve (11.5). Mira no **ato** — diagnosticar, prescrever, tranquilizar —
#: e nao no assunto: falar de vacina, consulta ou sintoma e o trabalho do agente; dizer
#: o que a pessoa deve fazer a respeito nao e.
_SCOPE_PATTERNS: Final[Mapping[str, re.Pattern[str]]] = {
    "prescricao": re.compile(
        r"\b(?:tome|tomar|de|dar|administre|administrar|aplique|aplicar|use|usar|passe)\b"
        r"[^.!?]{0,40}\b(?:mg|ml|comprimid\w*|remedi\w*|medicament\w*|antibiotic\w*|"
        r"vermifug\w*|pomada|anti-inflamator\w*|dipirona|ibuprofeno|paracetamol)\b"
        r"|\b\d+\s*(?:mg|ml)\b|\bde\s+\d+\s+em\s+\d+\s+horas\b"
    ),
    "diagnostico": re.compile(
        r"\b(?:provavelmente|deve\s+ser|parece\s+ser|pode\s+ser|e\s+um\s+caso\s+de|"
        r"se\s+trata\s+de|isso\s+e|suspeito\s+de|indica)\b[^.!?]{0,40}"
        r"\b(?:infecc\w*|viros\w*|alergi\w*|verme\w*|pulga\w*|fratur\w*|inflamac\w*|"
        r"gripe|doenc\w*|tumor|cancer|diabete\w*|dermatite|otite|gastrite|virus)\b"
        r"|\b(?:diagnostic|prognostic)\w*\s+(?:e|seria|provavel)\b"
    ),
    "tranquilizacao": re.compile(
        r"\bnao\s+(?:e|parece)\s+(?:nada\s+)?(?:grave|serio|preocupante|urgente)\b"
        r"|\bnao\s+precisa\s+(?:se\s+preocupar|levar|trazer|ir\s+ao)\b"
        r"|\b(?:pode|da\s+para)\s+esperar\s+(?:passar|melhorar|uns?\s+dias?)\b"
        r"|\bisso\s+passa\s+sozinho\b"
    ),
    "juridico_financeiro": re.compile(
        r"\bvoce\s+tem\s+direito\s+a\b|\b(?:pode|deve)\s+processar\b|\ba\s+lei\s+(?:diz|garante)\b"
        r"|\b(?:deduzir|dedutivel)\b[^.!?]{0,20}\bimposto\b|\breembolso\s+garantido\b"
        r"|\bplano\s+de\s+saude\s+(?:e\s+)?obrigad\w*\b"
    ),
}


def classify_scope(text: str) -> tuple[str, ...]:
    """Marcas de orientacao clinica, juridica ou financeira na resposta."""
    folded = fold(text)
    return tuple(name for name, pattern in _SCOPE_PATTERNS.items() if pattern.search(folded))


# ------------------------------------- precos -------------------------------------

_MONEY: Final[re.Pattern[str]] = re.compile(
    r"r\$\s*(?P<v>\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    r"|(?P<v2>\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*reais\b"
)


def _to_decimal(raw: str) -> Decimal | None:
    """`1.234,56` e `1234.56` viram o mesmo numero; qualquer outra coisa vira `None`."""
    cleaned = raw.strip()
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def extract_prices(text: str) -> frozenset[Decimal]:
    """Todo valor em reais citado no texto, normalizado."""
    found: set[Decimal] = set()
    for match in _MONEY.finditer(fold(text)):
        raw = match["v"] or match["v2"]
        value = _to_decimal(raw)
        if value is not None:
            found.add(value)
    return frozenset(found)


# --------------------------------------- PII ---------------------------------------

_LUHN_CANDIDATE: Final[re.Pattern[str]] = re.compile(r"(?<!\d)(?:\d[ .-]?){12,18}\d(?!\d)")
_CPF: Final[re.Pattern[str]] = re.compile(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)")
_CNPJ: Final[re.Pattern[str]] = re.compile(r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?!\d)")

#: O texto que substitui o dado. Curto e obvio: quem le o log precisa entender que ali
#: havia um documento, sem que o documento esteja la.
REDACTED: Final[str] = "[dado removido]"


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def redact_pii(text: str) -> tuple[str, tuple[str, ...]]:
    """Remove documento, CNPJ e cartao do texto. Devolve o texto e o que foi removido.

    Serve nos dois sentidos da 18.2: a resposta do agente nunca ecoa um documento, e o
    que a pessoa mandar espontaneamente e redigido antes de virar log.

    O cartao passa por Luhn antes de ser redigido — sem isso, qualquer sequencia longa
    de digitos (um numero de protocolo, um id) seria apagada. CPF e CNPJ nao tem essa
    folga: um numero de 11 digitos corridos numa resposta e documento, nao telefone
    (telefone sai formatado, com parenteses e hifen).
    """
    removed: list[str] = []

    def _mask(kind: str) -> Any:
        def replace(match: re.Match[str]) -> str:
            removed.append(kind)
            return REDACTED

        return replace

    def _mask_card(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if not (13 <= len(digits) <= 19 and _luhn(digits)):
            return match.group(0)
        removed.append("cartao")
        return REDACTED

    result = _CNPJ.sub(_mask("cnpj"), text)
    result = _LUHN_CANDIDATE.sub(_mask_card, result)
    result = _CPF.sub(_mask("cpf"), result)
    return result, tuple(dict.fromkeys(removed))


#: Pedir documento e proibido pela 18.2 (minimizacao). O padrao mira no **pedido**, nao
#: na palavra: "nao precisamos do seu CPF" e uma frase correta e nao pode ser barrada.
_PII_REQUEST: Final[re.Pattern[str]] = re.compile(
    r"\b(?:me\s+)?(?:passa|passe|informe|envie|manda|mande|digite|preciso\s+d[oa]|"
    r"qual\s+(?:e\s+)?[oa]|confirma\s+[oa]|poderia\s+(?:me\s+)?(?:passar|informar))\b"
    r"[^.!?]{0,30}\b(?:cpf|cnpj|rg|identidade|numero\s+do\s+cartao|cartao\s+de\s+credito|"
    r"dados\s+bancarios|conta\s+bancaria|agencia\s+e\s+conta|codigo\s+de\s+seguranca|cvv|"
    r"senha)\b"
)


# -------------------------------------- quebra --------------------------------------

#: Onde a quebra pode cair, da separacao mais natural para a menos.
_CUT_POINTS: Final[tuple[str, ...]] = ("\n\n", "\n", ". ", "! ", "? ", "; ", ", ", " ")


def split_message(text: str, limit: int) -> tuple[str, ...]:
    """Quebra a resposta em mensagens de ate `limit` caracteres.

    A quebra procura a fronteira mais natural que ainda sobra dentro do limite e nunca
    parte uma palavra. O corte tem um piso (`limit // 3`): sem ele, um texto com virgula
    logo no comeco viraria uma mensagem de tres palavras seguida de um parede de texto.
    """
    remaining = text.strip()
    if not remaining:
        return ()
    if limit <= 0:
        return (remaining,)

    chunks: list[str] = []
    while len(remaining) > limit:
        window = remaining[: limit + 1]
        cut = _best_cut(window, limit)
        chunk = remaining[:cut].strip()
        if not chunk:  # pragma: no cover - so acontece com separador no inicio
            break
        chunks.append(chunk)
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return tuple(chunks)


def _best_cut(window: str, limit: int) -> int:
    floor = limit // 3
    for separator in _CUT_POINTS:
        index = window.rfind(separator, 0, limit + 1)
        if index > floor:
            return index + len(separator)
    return limit


# --------------------------------- guardrail de saida ---------------------------------

#: Linguagem de oferta e de confirmacao. E ela que distingue "vou ver a quarta" de
#: "tenho quarta as 14h" — a primeira nao promete nada, a segunda promete um slot.
#:
#: Os verbos de fechamento (`fica`, `ficou`) precisam da excecao explicita porque a
#: mesma palavra abre a frase mais comum do atendimento que **nao** e promessa:
#: "a clinica fica na rua X" e "na sexta ficamos abertos das 8h as 18h".
_OFFER: Final[re.Pattern[str]] = re.compile(
    r"\b(?:tenho|temos|tem|ha|sobrou|restou|disponivel|disponiveis|vaga|vagas|livre|livres|"
    r"encaixe|encaixo|consigo|conseguimos|posso\s+(?:marcar|agendar|reservar|encaixar)|"
    r"agendado|agendei|agendada|agendo|marcado|marquei|marcada|marco|reservei|reservado|"
    r"reservo|separei|separo|anotei|anotado|garanti|garantido|"
    r"confirmado|confirmei|confirmada|confirmo|combinado|fechou|prontinho|"
    r"te\s+espero|espero\s+voce)\b"
    r"|\b(?:fica|ficou|ficamos|fico)\b"
    r"(?!\s+(?:aberto|aberta|abertos|fechado|fechada|tranquil|na\s+rua|no\s+numero|"
    r"em\s+frente|perto|ao\s+lado|proxim))"
)


@dataclass(slots=True)
class OutputGuardrail:
    """Passo 8 da 11.1: a ultima conferencia antes de a resposta virar mensagem."""

    def check(self, text: str, ctx: OutputContext) -> OutputDecision:
        persona = ctx.config.persona
        stripped = text.strip()
        if not stripped:
            return OutputDecision(verdict="pass", messages=())

        scope = classify_scope(stripped)
        if scope:
            # Recusa configurada substitui a resposta inteira. Nao adianta regenerar:
            # o modelo ja decidiu opinar, e a 11.5 manda trocar pelo texto do tenant.
            log.warning("guardrail_saida_escopo", marcas=list(scope))
            replacement = _refusal_text(ctx.config)
            return OutputDecision(
                verdict="replace",
                messages=split_message(replacement, persona.max_message_chars),
                violations=tuple(Violation(kind="escopo_clinico", detail=mark) for mark in scope),
            )

        redacted_text, removed = redact_pii(stripped)
        if removed:
            log.warning("guardrail_saida_pii_redigida", tipos=list(removed))

        violations: list[Violation] = []
        if _PII_REQUEST.search(fold(redacted_text)):
            violations.append(
                Violation(
                    kind="pii_solicitada",
                    detail="a resposta pede documento ou dado bancario",
                )
            )
        violations.extend(_temporal_violations(redacted_text, ctx))
        violations.extend(_price_violations(redacted_text, ctx))

        if violations:
            log.warning("guardrail_saida_descartou", motivos=[v.kind for v in violations])
            return OutputDecision(
                verdict="regenerate",
                messages=(),
                violations=tuple(violations),
                redacted=removed,
            )

        return OutputDecision(
            verdict="pass",
            messages=split_message(redacted_text, persona.max_message_chars),
            redacted=removed,
        )


def _refusal_text(config: TenantConfig) -> str:
    """Texto de recusa do tenant, com o gatilho de recusa na frente da mensagem generica."""
    for trigger in config.escalation.triggers:
        if trigger.action == "refuse_and_offer_appointment" and trigger.reply.strip():
            return trigger.reply.strip()
    generic = config.messages.out_of_scope.strip()
    return generic or (
        "Essa eu nao consigo responder por aqui — quem avalia isso e o profissional "
        "no atendimento. Quer que eu veja um horario?"
    )


def _temporal_violations(text: str, ctx: OutputContext) -> list[Violation]:
    """Confere cada afirmacao de data e hora contra o que o turno provou.

    Tres regras, com rigor decrescente e por um motivo:

    1. **Hora sempre.** Todo relogio citado tem de existir na evidencia. E a regra que
       fecha a alucinacao de horario, e ela nao tem excecao.
    2. **Slot quando ha promessa — ou quando a hora veio da agenda.** Um par data+hora
       precisa existir *junto* na evidencia em dois casos: quando a frase oferece ou
       confirma, e quando a hora citada e uma hora de slot real. O segundo caso existe
       porque e o unico jeito de pegar a mistura — a data de um slot verdadeiro com a
       hora de outro — mesmo que a frase esteja escrita sem promessa nenhuma.
       Sem nenhum dos dois, "na sexta abrimos as 8h" e horario de funcionamento.
    3. **Data so quando ha promessa.** "Te aviso amanha" nao promete horario nenhum;
       exigir prova ali transformaria conversa normal em handoff.
    """
    claims = pair_claims(find_mentions(text, today=ctx.today))
    if not claims:
        return []

    promises = bool(_OFFER.search(fold(text)))
    evidence = ctx.evidence
    agenda_times = {clock for _, clock in evidence.slots}
    violations: list[Violation] = []

    for claim in claims:
        if claim.times and not evidence.allows_time(claim.times):
            violations.append(_violation("hora_sem_evidencia", claim))
            continue
        if claim.kind == "slot":
            from_agenda = bool(claim.times & agenda_times)
            if (promises or from_agenda) and not evidence.allows_slot(claim.pairs):
                violations.append(_violation("slot_sem_evidencia", claim))
            continue
        if promises and claim.dates and not evidence.allows_date(claim.dates):
            violations.append(_violation("data_sem_evidencia", claim))
    return violations


def _violation(kind: ViolationKind, claim: TemporalClaim) -> Violation:
    return Violation(kind=kind, detail=f"{claim.raw.strip()!r} nao veio de nenhuma ferramenta")


def _price_violations(text: str, ctx: OutputContext) -> list[Violation]:
    """Numero precedido de `R$` so passa se existir no catalogo ou no resultado do RAG."""
    unknown = extract_prices(text) - ctx.prices
    return [
        Violation(kind="preco_sem_evidencia", detail=f"R$ {value} nao esta no catalogo")
        for value in sorted(unknown)
    ]


# ------------------------------- instrucao de correcao -------------------------------

_HINTS: Final[Mapping[str, str]] = {
    "hora_sem_evidencia": (
        "Voce citou um horario que nao veio de nenhuma ferramenta neste turno. "
        "Chame `check_availability` antes de mencionar qualquer horario, ou reescreva "
        "sem citar horario nenhum."
    ),
    "slot_sem_evidencia": (
        "Voce ofereceu uma combinacao de data e hora que nao esta em nenhum resultado de "
        "ferramenta. Ofereca apenas os slots que `check_availability` devolveu, do jeito "
        "que vieram."
    ),
    "data_sem_evidencia": (
        "Voce afirmou disponibilidade num dia que nao veio de ferramenta. Chame "
        "`check_availability` antes de prometer qualquer dia."
    ),
    "preco_sem_evidencia": (
        "Voce citou um preco que nao esta no catalogo nem na base de conhecimento. Use "
        "`list_services` ou `search_knowledge`, ou diga que vai confirmar o valor."
    ),
    "pii_solicitada": (
        "Voce pediu documento ou dado bancario. Isso e proibido: peca apenas os campos "
        "de cadastro configurados."
    ),
    "escopo_clinico": (
        "Voce deu orientacao clinica, juridica ou financeira. Recuse e ofereca um atendimento."
    ),
}


def regeneration_hint(violations: Sequence[Violation]) -> str:
    """Instrucao que volta ao modelo para ele reescrever a resposta.

    Vai como mensagem de usuario, nao como system prompt: o system prompt e prefixo
    cacheado (D-21) e mudar o prefixo no meio do turno joga fora o cache inteiro.
    """
    seen = dict.fromkeys(violation.kind for violation in violations)
    lines = [_HINTS[kind] for kind in seen if kind in _HINTS]
    detail = "; ".join(violation.detail for violation in violations[:3])
    return (
        "A sua resposta anterior foi descartada por uma verificacao automatica e nao "
        "chegou ao cliente.\n"
        + "\n".join(f"- {line}" for line in lines)
        + f"\nO que foi apontado: {detail}.\n"
        "Reescreva a resposta agora, corrigindo isso. Nao peca desculpas pela mensagem "
        "anterior — o cliente nao a viu."
    )


# ==================================== disjuntor ====================================


def circuit_breaker(limits: Limits, *, message_count: int, cost_usd: Decimal) -> str | None:
    """Disjuntor da conversa (11.5). Devolve o motivo, ou `None` se pode seguir.

    Conversa longa demais ou cara demais vira handoff **antes** de chamar o LLM: o teto
    existe justamente para nao ser ultrapassado enquanto se decide se foi ultrapassado.
    """
    if message_count > limits.max_messages_per_conversation:
        return "max_messages_per_conversation"
    if cost_usd > Decimal(str(limits.max_llm_cost_usd_per_conversation)):
        return "max_llm_cost_usd_per_conversation"
    return None


# ==================================== auxiliares ====================================


def business_hour_bounds(config: TenantConfig) -> tuple[tuple[time, time], ...]:
    """Bordas das janelas de funcionamento, para o agente poder dizer o proprio horario.

    So as bordas. O meio da janela nao prova disponibilidade nenhuma — e exatamente ali
    que mora a vaga inventada.
    """
    bounds: set[tuple[time, time]] = set()
    for windows in config.business_hours.weekly.values():
        for window in windows:
            bounds.add((time.fromisoformat(window.start), time.fromisoformat(window.end)))
    for exception in config.business_hours.exceptions:
        for window in exception.windows:
            bounds.add((time.fromisoformat(window.start), time.fromisoformat(window.end)))
    return tuple(sorted(bounds))


def prices_from(
    *, tool_results: Iterable[Any] = (), texts: Iterable[str] = ()
) -> frozenset[Decimal]:
    """Precos que o turno pode citar: os do catalogo, das tools e da base de conhecimento."""
    found: set[Decimal] = set()
    for content in tool_results:
        for raw in iter_strings(content):
            found |= extract_prices(raw)
    for text in texts:
        found |= extract_prices(text)
    return frozenset(found)
