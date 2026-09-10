"""Modelo de dados (secao 9 da spec).

Invariantes:
- Toda tabela com `tenant_id` tem RLS ligada (ver migration inicial). O ORM nao e a
  defesa contra vazamento entre tenants — o banco e.
- Todo instante e `TIMESTAMPTZ` (RNF-06). Nenhuma coluna `TIMESTAMP` sem timezone.
- `appointments` carrega a constraint de exclusao `appt_no_overlap`: a rede final
  contra overbooking, mesmo com bug de logica na camada de agendamento.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 1536


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[object, object]] = {
        dict[str, Any]: JSONB,
        list[str]: ARRAY(Text),
        datetime: DateTime(timezone=True),
        uuid.UUID: UUID(as_uuid=True),
    }


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def _tenant_fk(index: bool = True) -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=index,
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ============================== TENANCY ==============================


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _pk()
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    vertical: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = _created_at()

    locations: Mapped[list[Location]] = relationship(back_populates="tenant")


class Location(Base):
    __tablename__ = "locations"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(Text, nullable=False)  # IANA
    address: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    business_hours: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    tenant: Mapped[Tenant] = relationship(back_populates="locations")


# =============================== OFERTA ===============================


class Provider(Base):
    """Profissional que executa o atendimento. Uma agenda externa por profissional."""

    __tablename__ = "providers"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("locations.id")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(Text)
    working_hours: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    calendar_provider: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'google'")
    )
    calendar_id: Mapped[str | None] = mapped_column(Text)
    # Ponteiro para o secret store. O refresh token NUNCA e persistido aqui (secao 18.1).
    credentials_ref: Mapped[str | None] = mapped_column(Text)
    sync_token: Mapped[str | None] = mapped_column(Text)
    watch_channel_id: Mapped[str | None] = mapped_column(Text)
    watch_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class Resource(Base):
    """Ativo escasso nao-humano: sala, cadeira, equipamento."""

    __tablename__ = "resources"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("locations.id")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    calendar_id: Mapped[str | None] = mapped_column(Text)


class Service(Base):
    __tablename__ = "services"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    description: Mapped[str | None] = mapped_column(Text)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    buffer_before_min: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    buffer_after_min: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    price_cents: Mapped[int | None] = mapped_column(Integer)
    price_note: Mapped[str | None] = mapped_column(Text)
    modality: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'in_person'"))
    requires_intake: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    prep_instructions: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    __table_args__ = (
        CheckConstraint("duration_minutes > 0", name="service_duration_positive"),
        CheckConstraint("buffer_before_min >= 0 AND buffer_after_min >= 0", name="service_buffers"),
        CheckConstraint("modality IN ('in_person','online')", name="service_modality"),
    )


class ServiceProvider(Base):
    __tablename__ = "service_providers"

    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id", ondelete="CASCADE"), primary_key=True
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("providers.id", ondelete="CASCADE"), primary_key=True
    )
    duration_override_min: Mapped[int | None] = mapped_column(Integer)


class ServiceResource(Base):
    __tablename__ = "service_resources"

    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id", ondelete="CASCADE"), primary_key=True
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resources.id", ondelete="CASCADE"), primary_key=True
    )


# =============================== DEMANDA ===============================


class Contact(Base):
    """Pessoa do outro lado da conversa."""

    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    phone_e164: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    external_ref: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # LGPD (secao 18.2): base legal, data e canal do consentimento.
    consent: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (UniqueConstraint("tenant_id", "phone_e164", name="uq_contact_phone"),)


class Subject(Base):
    """Quem recebe o atendimento: o proprio contato, um dependente ou um pet."""

    __tablename__ = "subjects"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'self'"))
    name: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (CheckConstraint("kind IN ('self','dependent','pet')", name="subject_kind"),)


# ============================== CONVERSA ==============================


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    channel_thread: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    stage: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'greeting'"))
    collected: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    summary: Mapped[str | None] = mapped_column(Text)
    # Quantas mensagens ja entraram no `summary`. E o que faz o resumo rolante disparar
    # a cada 15 mensagens novas mesmo quando um turno grava duas de uma vez (secao 11.7).
    summary_message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    # Janela de 24h do WhatsApp: renovada a cada mensagem inbound (secao 14.1.4).
    service_window_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    silenced_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("tenant_id", "channel", "channel_thread", name="uq_conversation_thread"),
        CheckConstraint("channel IN ('whatsapp','web')", name="conversation_channel"),
        CheckConstraint("status IN ('active','handoff','closed')", name="conversation_status"),
        Index("ix_conversations_tenant_status", "tenant_id", "status"),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'text'"))
    content: Mapped[str | None] = mapped_column(Text)
    media_ref: Mapped[str | None] = mapped_column(Text)
    provider_msg_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    tool_calls: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    # Categoria de cobranca do WhatsApp: free | utility | marketing (secao 14.1.4).
    billing_category: Mapped[str | None] = mapped_column(Text)
    # `clock_timestamp()`, nao `now()`: `now()` e o instante do inicio da transacao, e
    # duas mensagens gravadas juntas ficariam com o mesmo `created_at`. Sem desempate
    # (a PK e UUID aleatorio), a janela de contexto do agente sairia fora de ordem.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )

    __table_args__ = (
        # Dedup da reentrega da Meta (RNF-04). NULL nao colide: mensagens de saida
        # ainda sem id do provider convivem sem conflito.
        UniqueConstraint("tenant_id", "provider_msg_id", name="uq_message_provider_id"),
        CheckConstraint("direction IN ('inbound','outbound')", name="message_direction"),
        CheckConstraint("author IN ('contact','agent','human')", name="message_author"),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )


# ============================= AGENDAMENTO =============================


class Hold(Base):
    """Reserva temporaria de um slot durante a conversa (secao 13.4, camada 1)."""

    __tablename__ = "holds"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("providers.id"), nullable=False
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resources.id")
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id"), nullable=False
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="hold_interval_valid"),
        CheckConstraint("status IN ('active','consumed','expired')", name="hold_status"),
        Index(
            "ix_holds_active_provider_window",
            "tenant_id",
            "provider_id",
            "starts_at",
            "ends_at",
            postgresql_where=text("status = 'active'"),
        ),
    )


class Appointment(Base):
    """Compromisso confirmado, espelhado como evento no calendario do profissional.

    A constraint `appt_no_overlap` (criada na migration, nao expressavel aqui) e a
    terceira e ultima camada contra overbooking.
    """

    __tablename__ = "appointments"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False
    )
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subjects.id")
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id")
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("providers.id"), nullable=False
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resources.id")
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id"), nullable=False
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("locations.id")
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'confirmed'"))
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'agent'"))
    external_event_id: Mapped[str | None] = mapped_column(Text)
    sync_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'synced'"))
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    intake: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    cancellation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_appointment_idempotency"),
        CheckConstraint("ends_at > starts_at", name="appointment_interval_valid"),
        CheckConstraint(
            "status IN ('confirmed','rescheduled','cancelled','no_show','completed')",
            name="appointment_status",
        ),
        CheckConstraint("source IN ('agent','human','external')", name="appointment_source"),
        CheckConstraint(
            "sync_status IN ('synced','pending_sync','sync_failed')", name="appointment_sync_status"
        ),
        Index("ix_appointments_provider_starts", "provider_id", "starts_at"),
        Index("ix_appointments_tenant_starts", "tenant_id", "starts_at"),
        Index("ix_appointments_external_event", "tenant_id", "external_event_id"),
    )


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    send_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    response: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("appointment_id", "kind", name="uq_reminder_appointment_kind"),
        CheckConstraint("status IN ('pending','sent','skipped','failed')", name="reminder_status"),
        Index(
            "ix_reminders_pending_send_at",
            "send_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )


class WaitlistEntry(Base):
    __tablename__ = "waitlist"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id"), nullable=False
    )
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("providers.id")
    )
    preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint(
            "status IN ('active','offered','booked','expired','cancelled')", name="waitlist_status"
        ),
    )


# ============================= CONHECIMENTO =============================


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)


# ============================== OPERACAO ==============================


class Handoff(Base):
    __tablename__ = "handoffs"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_by: Mapped[str] = mapped_column(Text, nullable=False)
    assigned_to: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    opened_at: Mapped[datetime] = _created_at()
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "triggered_by IN ('agent','contact','rule','human')", name="handoff_source"
        ),
        Index(
            "ix_handoffs_open",
            "tenant_id",
            "conversation_id",
            postgresql_where=text("closed_at IS NULL"),
        ),
    )


class AuditLog(Base):
    """Registro de toda acao com efeito externo (RF-30).

    Sem `ondelete` no tenant: o log sobrevive a remocao de dados operacionais.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (Index("ix_audit_tenant_created", "tenant_id", "created_at"),)


class AdminUser(Base):
    """Usuario do painel. Papeis: owner | attendant | support (Datamind)."""

    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    email: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'attendant'"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_admin_user_email"),
        CheckConstraint("role IN ('owner','attendant','support')", name="admin_user_role"),
    )


#: Tabelas que carregam `tenant_id` e portanto exigem RLS (RNF-03).
#: A migration e o teste de isolamento leem esta lista.
#: Manter em sincronia com os modelos — o teste falha se divergir.
TENANT_SCOPED_TABLES: tuple[str, ...] = (
    "locations",
    "providers",
    "resources",
    "services",
    "contacts",
    "subjects",
    "conversations",
    "messages",
    "holds",
    "appointments",
    "reminders",
    "waitlist",
    "knowledge_documents",
    "knowledge_chunks",
    "handoffs",
    "audit_log",
    "admin_users",
)
