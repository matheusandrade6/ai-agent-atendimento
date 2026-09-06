"""Row Level Security por tenant (RNF-03)

Duas decisoes que fazem o isolamento valer de verdade:

1. `FORCE ROW LEVEL SECURITY`. Sem ele, o dono da tabela ignora a politica — e aqui o
   dono e o proprio usuario da aplicacao, o que tornaria a RLS decorativa.

2. `current_setting('app.tenant_id', true)` com o segundo argumento em `true` devolve
   NULL quando a variavel nao foi definida, em vez de levantar erro. Como a comparacao
   com NULL e falsa, o padrao passa a ser **nao ver nada**: esquecer de abrir o contexto
   de tenant nao vaza dado, so devolve zero linha.

`app.bypass_rls = 'on'` e a valvula para migrations, onboarding, roteamento de webhook
(que descobre o tenant antes de te-lo) e jobs que varrem a base inteira. Ela e explicita
e nomeada justamente para aparecer em code review.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Manter em sincronia com app.domain.models.TENANT_SCOPED_TABLES.
# O teste tests/integration/test_rls.py falha se as duas listas divergirem.
TENANT_SCOPED_TABLES = (
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

_PREDICATE = (
    "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid "
    "OR NULLIF(current_setting('app.bypass_rls', true), '') = 'on'"
)


def upgrade() -> None:
    for table in TENANT_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING ({_PREDICATE})
            WITH CHECK ({_PREDICATE})
            """
        )


def downgrade() -> None:
    for table in TENANT_SCOPED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
