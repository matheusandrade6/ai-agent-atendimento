"""Configuracao por tenant (secao 10 da spec).

Toda personalizacao de cliente cabe aqui. Se nao couber, vira campo novo deste schema
ou nao e feito — nunca `if tenant == 'x'` no codigo.

Segredos
--------
O YAML usa `${VAR}` e o **valor bruto e o que vai para o banco**. A resolucao acontece
na leitura (`TenantConfig.resolved()`), nao na gravacao: assim `tenants.config` nunca
guarda `app secret` do WhatsApp nem token nenhum (secao 18.1).
"""

from __future__ import annotations

import os
import re
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS: tuple[Weekday, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


class _Strict(BaseModel):
    """Base de todos os blocos: campo desconhecido no YAML e erro, nao silencio."""

    model_config = ConfigDict(extra="forbid")


# ------------------------------- identidade -------------------------------


class Identity(_Strict):
    name: str
    vertical: str
    about: str = ""


class Vocabulary(_Strict):
    prefer: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)


class Persona(_Strict):
    agent_name: str
    # Exigencia etica e de LGPD (secao 18.4): o agente nunca simula ser humano.
    introduce_as_ai: bool = True
    tone: str = "cordial, direto"
    address_form: Literal["voce", "senhor_senhora"] = "voce"
    emojis: Literal["never", "sparingly", "often"] = "sparingly"
    max_message_chars: int = Field(default=600, ge=100, le=4000)
    signature: str | None = None
    vocabulary: Vocabulary = Field(default_factory=Vocabulary)


# --------------------------------- canais ---------------------------------


class WhatsAppTemplate(_Strict):
    """Template utility aprovado. Sem mapeamento aqui, nao ha envio proativo."""

    name: str
    language: str = "pt_BR"
    variables: list[str] = Field(default_factory=list)


class WhatsAppChannel(_Strict):
    enabled: bool = True
    phone_number_id: str
    waba_id: str = ""
    debounce_seconds: int = Field(default=6, ge=0, le=60)
    templates: dict[str, WhatsAppTemplate] = Field(default_factory=dict)


class WebChannel(_Strict):
    enabled: bool = False
    allowed_origins: list[str] = Field(default_factory=list)
    greeting: str = ""
    debounce_seconds: int = Field(default=2, ge=0, le=60)

    @model_validator(mode="after")
    def _origins_required_when_enabled(self) -> Self:
        if self.enabled and not self.allowed_origins:
            raise ValueError("channels.web.allowed_origins e obrigatorio com o widget habilitado")
        return self


class Channels(_Strict):
    whatsapp: WhatsAppChannel | None = None
    web: WebChannel = Field(default_factory=WebChannel)

    @model_validator(mode="after")
    def _at_least_one(self) -> Self:
        whatsapp_on = self.whatsapp is not None and self.whatsapp.enabled
        if not whatsapp_on and not self.web.enabled:
            raise ValueError("o tenant precisa de pelo menos um canal habilitado")
        return self


# ------------------------------- agendamento -------------------------------


class ReminderRule(_Strict):
    kind: str
    offset_hours: float = Field(description="Negativo = antes do horario do agendamento")
    ask_confirmation: bool = False
    template: str | None = Field(
        default=None,
        description="Chave em channels.whatsapp.templates, usada com a janela de 24h fechada",
    )

    @model_validator(mode="after")
    def _offset_before_appointment(self) -> Self:
        if self.offset_hours >= 0:
            raise ValueError("offset_hours precisa ser negativo (o lembrete vem antes)")
        return self


class SchedulingRules(_Strict):
    calendar_provider: Literal["google", "internal"] = "google"
    min_lead_time_minutes: int = Field(default=120, ge=0)
    max_horizon_days: int = Field(default=45, ge=1, le=365)
    slots_per_offer: int = Field(default=3, ge=1, le=10)
    slot_granularity_minutes: int = Field(default=15, ge=5, le=120)
    hold_ttl_minutes: int = Field(default=10, ge=1, le=120)
    allow_reschedule: bool = True
    reschedule_min_notice_hours: float = Field(default=4, ge=0)
    allow_cancel: bool = True
    cancel_min_notice_hours: float = Field(default=4, ge=0)
    reminders: list[ReminderRule] = Field(default_factory=list)
    waitlist_enabled: bool = False
    # Deltas verticais (secao 22.2)
    allow_service_bundle: bool = False
    allow_recurring: bool = False
    recurrence_max_sessions: int = Field(default=1, ge=1, le=52)
    recurrence_pattern: Literal["weekly_same_slot", "biweekly_same_slot"] | None = None

    @model_validator(mode="after")
    def _unique_reminder_kinds(self) -> Self:
        kinds = [r.kind for r in self.reminders]
        if len(kinds) != len(set(kinds)):
            raise ValueError("scheduling.reminders tem `kind` repetido")
        return self


# ---------------------------------- intake ----------------------------------

FieldType = Literal[
    "string", "boolean", "integer", "enum", "enum_ref", "service_ref", "provider_ref", "date"
]


class IntakeField(_Strict):
    key: str
    label: str = ""
    type: FieldType = "string"
    options: list[str] = Field(default_factory=list)
    source: str | None = Field(
        default=None, description="Para type=enum_ref: nome da lista na base do tenant"
    )
    optional: bool = False
    allow_multiple: bool = False
    affects_duration: bool = False
    on_other: Literal["accept", "escalate"] = "accept"

    @model_validator(mode="after")
    def _enum_needs_options(self) -> Self:
        if self.type == "enum" and not self.options:
            raise ValueError(f"campo '{self.key}': type=enum exige `options`")
        if self.type == "enum_ref" and not self.source:
            raise ValueError(f"campo '{self.key}': type=enum_ref exige `source`")
        return self


class ConditionalIntake(_Strict):
    when: str = Field(description="Expressao avaliada sobre os campos ja coletados")
    require: list[IntakeField]


class IntakeConfig(_Strict):
    required: list[IntakeField] = Field(default_factory=list)
    conditional: list[ConditionalIntake] = Field(default_factory=list)
    ask_style: Literal["one_at_a_time", "grouped"] = "one_at_a_time"
    max_questions_before_offer: int = Field(default=5, ge=1, le=15)

    @model_validator(mode="after")
    def _unique_keys(self) -> Self:
        keys = [f.key for f in self.required]
        for block in self.conditional:
            keys.extend(f.key for f in block.require)
        duplicated = {k for k in keys if keys.count(k) > 1}
        if duplicated:
            raise ValueError(f"intake com chave repetida: {', '.join(sorted(duplicated))}")
        return self

    def field_keys(self) -> list[str]:
        keys = [f.key for f in self.required]
        for block in self.conditional:
            keys.extend(f.key for f in block.require)
        return keys


# -------------------------------- escalonamento --------------------------------


class NotifyTarget(_Strict):
    channel: Literal["whatsapp", "email", "panel"]
    to: str


class EscalationTrigger(_Strict):
    id: str
    match_keywords: list[str] = Field(default_factory=list)
    match_intent: str | None = None
    condition: str | None = None
    action: Literal[
        "escalate", "escalate_immediately", "refuse_and_offer_appointment", "refuse"
    ] = "escalate"
    reply: str = ""

    @model_validator(mode="after")
    def _needs_a_matcher(self) -> Self:
        if not (self.match_keywords or self.match_intent or self.condition):
            raise ValueError(
                f"trigger '{self.id}': precisa de match_keywords, match_intent ou condition"
            )
        return self


class EscalationConfig(_Strict):
    business_hours_only: bool = False
    notify: list[NotifyTarget] = Field(default_factory=list)
    triggers: list[EscalationTrigger] = Field(default_factory=list)
    handoff_silence_minutes: int = Field(default=120, ge=1, le=1440)

    @model_validator(mode="after")
    def _unique_trigger_ids(self) -> Self:
        ids = [t.id for t in self.triggers]
        if len(ids) != len(set(ids)):
            raise ValueError("escalation.triggers tem `id` repetido")
        return self


# ---------------------------------- mensagens ----------------------------------


class MessageTemplates(_Strict):
    model_config = ConfigDict(extra="allow")  # tenant pode acrescentar mensagens proprias

    greeting_new: str = ""
    greeting_returning: str = ""
    out_of_scope: str = ""
    confirmation: str = ""


class Limits(_Strict):
    max_llm_cost_usd_per_conversation: float = Field(default=0.15, gt=0)
    max_messages_per_conversation: int = Field(default=60, ge=1)
    monthly_budget_alert_usd: float = Field(default=60.0, gt=0)


# ------------------------------ horario de funcionamento ------------------------------


class TimeWindow(_Strict):
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")

    @model_validator(mode="after")
    def _end_after_start(self) -> Self:
        if self.end <= self.start:
            raise ValueError(f"janela invalida: {self.start}-{self.end}")
        return self


class BusinessHoursException(_Strict):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    closed: bool = False
    windows: list[TimeWindow] = Field(default_factory=list)

    @model_validator(mode="after")
    def _closed_or_windows(self) -> Self:
        if self.closed and self.windows:
            raise ValueError(f"excecao {self.date}: `closed` e `windows` sao mutuamente exclusivos")
        if not self.closed and not self.windows:
            raise ValueError(f"excecao {self.date}: informe `closed: true` ou `windows`")
        return self


class BusinessHours(_Strict):
    timezone: str = "America/Sao_Paulo"
    weekly: dict[Weekday, list[TimeWindow]] = Field(default_factory=dict)
    exceptions: list[BusinessHoursException] = Field(default_factory=list)

    @model_validator(mode="after")
    def _windows_do_not_overlap(self) -> Self:
        for day, windows in self.weekly.items():
            ordered = sorted(windows, key=lambda w: w.start)
            for earlier, later in pairwise(ordered):
                if later.start < earlier.end:
                    raise ValueError(f"janelas sobrepostas em {day}: {earlier.end} > {later.start}")
        return self


# ------------------------------------ raiz ------------------------------------


class TenantConfig(_Strict):
    identity: Identity
    persona: Persona
    channels: Channels
    scheduling: SchedulingRules = Field(default_factory=SchedulingRules)
    intake: IntakeConfig = Field(default_factory=IntakeConfig)
    escalation: EscalationConfig = Field(default_factory=EscalationConfig)
    messages: MessageTemplates = Field(default_factory=MessageTemplates)
    limits: Limits = Field(default_factory=Limits)
    business_hours: BusinessHours = Field(default_factory=BusinessHours)

    @model_validator(mode="after")
    def _reminders_have_templates(self) -> Self:
        """Lembrete sem template mapeado nao sai com a janela de 24h fechada.

        Falhar aqui, no onboarding, e muito mais barato do que descobrir isso na
        vespera do primeiro lembrete (secao 14.1.4).
        """
        whatsapp = self.channels.whatsapp
        if whatsapp is None or not whatsapp.enabled:
            return self
        for reminder in self.scheduling.reminders:
            if reminder.template and reminder.template not in whatsapp.templates:
                raise ValueError(
                    f"lembrete '{reminder.kind}' aponta para o template "
                    f"'{reminder.template}', que nao esta em channels.whatsapp.templates"
                )
        return self

    def resolved(self, env: dict[str, str] | None = None) -> TenantConfig:
        """Devolve uma copia com os `${VAR}` substituidos pelo ambiente.

        Use no momento do uso, nunca antes de gravar em `tenants.config`.
        """
        source = os.environ if env is None else env
        return TenantConfig.model_validate(_expand(self.model_dump(mode="python"), source))

    def raw_dict(self) -> dict[str, Any]:
        """Forma serializavel para `tenants.config`, com os `${VAR}` preservados."""
        return self.model_dump(mode="json")


def _expand(value: Any, env: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: str(env.get(m.group(1), "")), value)
    if isinstance(value, dict):
        return {k: _expand(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, env) for v in value]
    return value


class TenantConfigError(ValueError):
    """Config invalida, com o caminho do campo no arquivo."""


def load_tenant_config(path: str | Path) -> TenantConfig:
    """Le e valida um YAML de tenant. Nao resolve `${VAR}`."""
    file_path = Path(path)
    try:
        data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise TenantConfigError(f"{file_path}: YAML invalido — {exc}") from exc

    try:
        return TenantConfig.model_validate(data)
    except Exception as exc:
        raise TenantConfigError(f"{file_path}: {exc}") from exc


def load_all(directory: str | Path) -> dict[str, TenantConfig]:
    """Carrega todos os tenants de um diretorio. Arquivos com `_` na frente sao gabaritos."""
    base = Path(directory)
    configs: dict[str, TenantConfig] = {}
    for file_path in sorted(base.glob("*.yaml")):
        if file_path.stem.startswith("_"):
            continue
        configs[file_path.stem] = load_tenant_config(file_path)
    return configs
