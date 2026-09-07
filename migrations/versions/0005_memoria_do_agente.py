"""Memoria do agente: contador do resumo e ordem estavel das mensagens

Duas mudancas, as duas a servico da janela de contexto da secao 11.7.

1. `conversations.summary_message_count`. O resumo e regenerado a cada 15 mensagens
   novas. Sem guardar quantas ja entraram no resumo, o gatilho teria que ser
   `total % 15 == 0` — que erra sempre que um turno grava duas mensagens de uma vez, e a
   conversa fica sem resumo ate a proxima coincidencia.

2. `messages.created_at` passa a usar `clock_timestamp()`. `now()` no Postgres e o
   instante do **inicio da transacao**: duas mensagens gravadas na mesma transacao —
   uma rajada que chega num unico payload da Meta, por exemplo — recebem o mesmo
   `created_at`. Como a PK e um UUID aleatorio, nao existe criterio de desempate, e a
   janela enviada ao modelo pode sair fora de ordem: a resposta do agente antes da
   pergunta do cliente. `clock_timestamp()` e o instante da propria linha.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "summary_message_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.alter_column("messages", "created_at", server_default=sa.text("clock_timestamp()"))


def downgrade() -> None:
    op.alter_column("messages", "created_at", server_default=sa.text("now()"))
    op.drop_column("conversations", "summary_message_count")
