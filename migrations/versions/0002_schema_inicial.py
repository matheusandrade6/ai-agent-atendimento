"""Schema inicial (secao 9 da spec)

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql as pg

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid_pk() -> sa.Column[object]:
    return sa.Column(
        "id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def _tenant_fk(nullable: bool = False) -> sa.Column[object]:
    return sa.Column(
        "tenant_id",
        pg.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=nullable,
    )


def _created_at() -> sa.Column[object]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def upgrade() -> None:
    # ------------------------------ TENANCY ------------------------------
    op.create_table(
        "tenants",
        _uuid_pk(),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("vertical", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("config", pg.JSONB(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        _created_at(),
    )

    op.create_table(
        "locations",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("address", pg.JSONB()),
        sa.Column("business_hours", pg.JSONB(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.create_index("ix_locations_tenant_id", "locations", ["tenant_id"])

    # ------------------------------- OFERTA -------------------------------
    op.create_table(
        "providers",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("location_id", pg.UUID(as_uuid=True), sa.ForeignKey("locations.id")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text()),
        sa.Column("role", sa.Text()),
        sa.Column("working_hours", pg.JSONB(), nullable=False),
        sa.Column(
            "calendar_provider", sa.Text(), nullable=False, server_default=sa.text("'google'")
        ),
        sa.Column("calendar_id", sa.Text()),
        sa.Column("credentials_ref", sa.Text()),
        sa.Column("sync_token", sa.Text()),
        sa.Column("watch_channel_id", sa.Text()),
        sa.Column("watch_expires_at", sa.DateTime(timezone=True)),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.create_index("ix_providers_tenant_id", "providers", ["tenant_id"])

    op.create_table(
        "resources",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("location_id", pg.UUID(as_uuid=True), sa.ForeignKey("locations.id")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("calendar_id", sa.Text()),
    )
    op.create_index("ix_resources_tenant_id", "resources", ["tenant_id"])

    op.create_table(
        "services",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("aliases", pg.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("description", sa.Text()),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("buffer_before_min", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("buffer_after_min", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("price_cents", sa.Integer()),
        sa.Column("price_note", sa.Text()),
        sa.Column("modality", sa.Text(), nullable=False, server_default=sa.text("'in_person'")),
        sa.Column(
            "requires_intake",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("prep_instructions", sa.Text()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.CheckConstraint("duration_minutes > 0", name="service_duration_positive"),
        sa.CheckConstraint(
            "buffer_before_min >= 0 AND buffer_after_min >= 0", name="service_buffers"
        ),
        sa.CheckConstraint("modality IN ('in_person','online')", name="service_modality"),
    )
    op.create_index("ix_services_tenant_id", "services", ["tenant_id"])

    op.create_table(
        "service_providers",
        sa.Column(
            "service_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("services.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "provider_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("providers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("duration_override_min", sa.Integer()),
    )

    op.create_table(
        "service_resources",
        sa.Column(
            "service_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("services.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "resource_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("resources.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    # ------------------------------- DEMANDA -------------------------------
    op.create_table(
        "contacts",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("phone_e164", sa.Text()),
        sa.Column("name", sa.Text()),
        sa.Column("email", sa.Text()),
        sa.Column("external_ref", sa.Text()),
        sa.Column("attributes", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("consent", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        _created_at(),
        sa.UniqueConstraint("tenant_id", "phone_e164", name="uq_contact_phone"),
    )
    op.create_index("ix_contacts_tenant_id", "contacts", ["tenant_id"])

    op.create_table(
        "subjects",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column(
            "contact_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'self'")),
        sa.Column("name", sa.Text()),
        sa.Column("attributes", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("kind IN ('self','dependent','pet')", name="subject_kind"),
    )
    op.create_index("ix_subjects_tenant_id", "subjects", ["tenant_id"])

    # ------------------------------ CONVERSA ------------------------------
    op.create_table(
        "conversations",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column(
            "contact_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("channel_thread", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("stage", sa.Text(), nullable=False, server_default=sa.text("'greeting'")),
        sa.Column("collected", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("summary", sa.Text()),
        sa.Column("service_window_expires_at", sa.DateTime(timezone=True)),
        sa.Column("silenced_until", sa.DateTime(timezone=True)),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        _created_at(),
        sa.UniqueConstraint(
            "tenant_id", "channel", "channel_thread", name="uq_conversation_thread"
        ),
        sa.CheckConstraint("channel IN ('whatsapp','web')", name="conversation_channel"),
        sa.CheckConstraint("status IN ('active','handoff','closed')", name="conversation_status"),
    )
    op.create_index("ix_conversations_tenant_id", "conversations", ["tenant_id"])
    op.create_index("ix_conversations_tenant_status", "conversations", ["tenant_id", "status"])

    op.create_table(
        "messages",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column(
            "conversation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False, server_default=sa.text("'text'")),
        sa.Column("content", sa.Text()),
        sa.Column("media_ref", sa.Text()),
        sa.Column("provider_msg_id", sa.Text()),
        sa.Column("status", sa.Text()),
        sa.Column("tool_calls", pg.JSONB()),
        sa.Column("tokens_in", sa.Integer()),
        sa.Column("tokens_out", sa.Integer()),
        sa.Column("cost_usd", sa.Numeric(10, 6)),
        sa.Column("billing_category", sa.Text()),
        _created_at(),
        sa.UniqueConstraint("tenant_id", "provider_msg_id", name="uq_message_provider_id"),
        sa.CheckConstraint("direction IN ('inbound','outbound')", name="message_direction"),
        sa.CheckConstraint("author IN ('contact','agent','human')", name="message_author"),
    )
    op.create_index("ix_messages_tenant_id", "messages", ["tenant_id"])
    op.create_index(
        "ix_messages_conversation_created", "messages", ["conversation_id", "created_at"]
    )

    # ----------------------------- AGENDAMENTO -----------------------------
    op.create_table(
        "holds",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column(
            "conversation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider_id", pg.UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False
        ),
        sa.Column("resource_id", pg.UUID(as_uuid=True), sa.ForeignKey("resources.id")),
        sa.Column(
            "service_id", pg.UUID(as_uuid=True), sa.ForeignKey("services.id"), nullable=False
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        _created_at(),
        sa.CheckConstraint("ends_at > starts_at", name="hold_interval_valid"),
        sa.CheckConstraint("status IN ('active','consumed','expired')", name="hold_status"),
    )
    op.create_index("ix_holds_tenant_id", "holds", ["tenant_id"])
    op.create_index(
        "ix_holds_active_provider_window",
        "holds",
        ["tenant_id", "provider_id", "starts_at", "ends_at"],
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "appointments",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column(
            "contact_id", pg.UUID(as_uuid=True), sa.ForeignKey("contacts.id"), nullable=False
        ),
        sa.Column("subject_id", pg.UUID(as_uuid=True), sa.ForeignKey("subjects.id")),
        sa.Column("conversation_id", pg.UUID(as_uuid=True), sa.ForeignKey("conversations.id")),
        sa.Column(
            "provider_id", pg.UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False
        ),
        sa.Column("resource_id", pg.UUID(as_uuid=True), sa.ForeignKey("resources.id")),
        sa.Column(
            "service_id", pg.UUID(as_uuid=True), sa.ForeignKey("services.id"), nullable=False
        ),
        sa.Column("location_id", pg.UUID(as_uuid=True), sa.ForeignKey("locations.id")),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'confirmed'")),
        sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'agent'")),
        sa.Column("external_event_id", sa.Text()),
        sa.Column("sync_status", sa.Text(), nullable=False, server_default=sa.text("'synced'")),
        sa.Column("idempotency_key", sa.Text()),
        sa.Column("intake", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("cancellation", pg.JSONB()),
        _created_at(),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_appointment_idempotency"),
        sa.CheckConstraint("ends_at > starts_at", name="appointment_interval_valid"),
        sa.CheckConstraint(
            "status IN ('confirmed','rescheduled','cancelled','no_show','completed')",
            name="appointment_status",
        ),
        sa.CheckConstraint("source IN ('agent','human','external')", name="appointment_source"),
        sa.CheckConstraint(
            "sync_status IN ('synced','pending_sync','sync_failed')",
            name="appointment_sync_status",
        ),
    )
    op.create_index("ix_appointments_tenant_id", "appointments", ["tenant_id"])
    op.create_index("ix_appointments_provider_starts", "appointments", ["provider_id", "starts_at"])
    op.create_index("ix_appointments_tenant_starts", "appointments", ["tenant_id", "starts_at"])
    op.create_index(
        "ix_appointments_external_event", "appointments", ["tenant_id", "external_event_id"]
    )

    # A rede final contra overbooking (secao 13.4, camada 3). Mesmo com bug na logica
    # de hold ou falha do advisory lock, o banco recusa a segunda insercao.
    op.execute(
        """
        ALTER TABLE appointments ADD CONSTRAINT appt_no_overlap
        EXCLUDE USING gist (
            provider_id WITH =,
            tstzrange(starts_at, ends_at) WITH &&
        ) WHERE (status IN ('confirmed','rescheduled'))
        """
    )

    op.create_table(
        "reminders",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column(
            "appointment_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("send_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("response", sa.Text()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("appointment_id", "kind", name="uq_reminder_appointment_kind"),
        sa.CheckConstraint(
            "status IN ('pending','sent','skipped','failed')", name="reminder_status"
        ),
    )
    op.create_index("ix_reminders_tenant_id", "reminders", ["tenant_id"])
    op.create_index(
        "ix_reminders_pending_send_at",
        "reminders",
        ["send_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "waitlist",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column(
            "contact_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "service_id", pg.UUID(as_uuid=True), sa.ForeignKey("services.id"), nullable=False
        ),
        sa.Column("provider_id", pg.UUID(as_uuid=True), sa.ForeignKey("providers.id")),
        sa.Column("preferences", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        _created_at(),
        sa.CheckConstraint(
            "status IN ('active','offered','booked','expired','cancelled')",
            name="waitlist_status",
        ),
    )
    op.create_index("ix_waitlist_tenant_id", "waitlist", ["tenant_id"])

    # ----------------------------- CONHECIMENTO -----------------------------
    op.create_table(
        "knowledge_documents",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source", sa.Text()),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_knowledge_documents_tenant_id", "knowledge_documents", ["tenant_id"])

    op.create_table(
        "knowledge_chunks",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column(
            "document_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False),
    )
    op.create_index("ix_knowledge_chunks_tenant_id", "knowledge_chunks", ["tenant_id"])
    op.create_index(
        "ix_knowledge_chunks_embedding",
        "knowledge_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    # ------------------------------ OPERACAO ------------------------------
    op.create_table(
        "handoffs",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column(
            "conversation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("triggered_by", sa.Text(), nullable=False),
        sa.Column("assigned_to", sa.Text()),
        sa.Column("summary", sa.Text()),
        sa.Column("notified_at", sa.DateTime(timezone=True)),
        sa.Column(
            "opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "triggered_by IN ('agent','contact','rule','human')", name="handoff_source"
        ),
    )
    op.create_index("ix_handoffs_tenant_id", "handoffs", ["tenant_id"])
    op.create_index(
        "ix_handoffs_open",
        "handoffs",
        ["tenant_id", "conversation_id"],
        postgresql_where=sa.text("closed_at IS NULL"),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity", sa.Text()),
        sa.Column("entity_id", pg.UUID(as_uuid=True)),
        sa.Column("payload", pg.JSONB()),
        _created_at(),
    )
    op.create_index("ix_audit_log_tenant_id", "audit_log", ["tenant_id"])
    op.create_index("ix_audit_tenant_created", "audit_log", ["tenant_id", "created_at"])

    op.create_table(
        "admin_users",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("name", sa.Text()),
        sa.Column("role", sa.Text(), nullable=False, server_default=sa.text("'attendant'")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        _created_at(),
        sa.UniqueConstraint("tenant_id", "email", name="uq_admin_user_email"),
        sa.CheckConstraint("role IN ('owner','attendant','support')", name="admin_user_role"),
    )
    op.create_index("ix_admin_users_tenant_id", "admin_users", ["tenant_id"])


def downgrade() -> None:
    for table in (
        "admin_users",
        "audit_log",
        "handoffs",
        "knowledge_chunks",
        "knowledge_documents",
        "waitlist",
        "reminders",
        "appointments",
        "holds",
        "messages",
        "conversations",
        "subjects",
        "contacts",
        "service_resources",
        "service_providers",
        "services",
        "resources",
        "providers",
        "locations",
        "tenants",
    ):
        op.drop_table(table)
