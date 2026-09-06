"""Privilegios do papel de aplicacao

As migrations rodam como dono das tabelas (superusuario). A aplicacao roda como
`datamind_app`, que e NOSUPERUSER e NOBYPASSRLS — condicao para a RLS valer de fato,
ja que superusuario ignora policy mesmo com FORCE (ver docs/DECISOES.md, D-09).

A migration e tolerante a ausencia do papel: em ambientes onde o provisionamento cria
outro nome, ela nao quebra o `upgrade head`.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "datamind_app"


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT USAGE ON SCHEMA public TO {APP_ROLE}';
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE '
                        'ON ALL TABLES IN SCHEMA public TO {APP_ROLE}';
                EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}';
            ELSE
                RAISE NOTICE
                    'papel {APP_ROLE} ausente: conceda os privilegios manualmente';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}';
                EXECUTE 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE}';
                EXECUTE 'REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}';
            END IF;
        END
        $$;
        """
    )
