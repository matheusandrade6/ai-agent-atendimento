"""Extensoes do Postgres

`btree_gist` habilita a constraint de exclusao que impede overbooking (secao 13.4).
`vector` guarda os embeddings da base de conhecimento (secao 11.6).
`pgcrypto` garante `gen_random_uuid()` mesmo em Postgres anterior ao 13.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXTENSIONS = ("pgcrypto", "btree_gist", "vector")


def upgrade() -> None:
    for ext in EXTENSIONS:
        op.execute(f'CREATE EXTENSION IF NOT EXISTS "{ext}"')


def downgrade() -> None:
    # Extensoes nao sao removidas: outras estruturas do banco podem depender delas
    # e derruba-las e mais destrutivo do que reversivel.
    pass
