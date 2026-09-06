"""Isolamento entre tenants (RNF-03) e rede anti-overbooking (secao 13.4, camada 3).

Estes dois testes existem porque as falhas que eles cobrem sao as de maior impacto do
projeto: vazamento entre clientes e agendamento duplo. Ambos rodam contra Postgres real —
nao ha como provar RLS nem constraint de exclusao com mock.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.domain.models import TENANT_SCOPED_TABLES

pytestmark = pytest.mark.integration


def _set_tenant(conn: Connection, tenant_id: uuid.UUID | None) -> None:
    conn.execute(
        sa.text("SELECT set_config('app.tenant_id', :v, false)"),
        {"v": str(tenant_id) if tenant_id else ""},
    )
    conn.execute(sa.text("SELECT set_config('app.bypass_rls', '', false)"))


def _bypass(conn: Connection) -> None:
    conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))


def _seed_tenant(conn: Connection, slug: str) -> uuid.UUID:
    return conn.execute(
        sa.text(
            "INSERT INTO tenants (slug, name, vertical, config) "
            "VALUES (:slug, :slug, 'veterinaria', '{}'::jsonb) RETURNING id"
        ),
        {"slug": slug},
    ).scalar_one()


@pytest.fixture
def two_tenants(sync_engine: sa.Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """Dois tenants com um contato cada. Removidos ao fim, aconteca o que acontecer."""
    marker = uuid.uuid4().hex[:8]
    with sync_engine.begin() as conn:
        _bypass(conn)
        tenant_a = _seed_tenant(conn, f"tenant-a-{marker}")
        tenant_b = _seed_tenant(conn, f"tenant-b-{marker}")
        for tenant_id, name in ((tenant_a, "Carla"), (tenant_b, "Bruno")):
            conn.execute(
                sa.text("INSERT INTO contacts (tenant_id, phone_e164, name) VALUES (:t, :p, :n)"),
                {"t": tenant_id, "p": f"+5511{marker}{str(tenant_id)[:4]}", "n": name},
            )
    yield tenant_a, tenant_b
    with sync_engine.begin() as conn:
        _bypass(conn)
        conn.execute(
            sa.text("DELETE FROM tenants WHERE id = ANY(:ids)"),
            {"ids": [tenant_a, tenant_b]},
        )


# ------------------------------ isolamento ------------------------------


def test_aplicacao_nao_conecta_como_superusuario(sync_engine: sa.Engine) -> None:
    """A premissa de que toda a RLS depende.

    Superusuario ignora policy por completo — inclusive com FORCE, que so alcanca o
    dono da tabela. Se a aplicacao conectar com um papel privilegiado, todos os outros
    testes deste arquivo passam a testar nada. Por isso este vem primeiro.
    """
    with sync_engine.connect() as conn:
        role = conn.execute(
            sa.text(
                "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
        ).one()

    assert not role.rolsuper, (
        f"a aplicacao conecta como '{role.rolname}', que e SUPERUSER: a RLS nao vale nada. "
        "Use o papel datamind_app (ver docs/DECISOES.md, D-09)."
    )
    assert not role.rolbypassrls, f"'{role.rolname}' tem BYPASSRLS"


def test_tenant_a_nao_ve_linha_de_b(
    sync_engine: sa.Engine, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, tenant_b = two_tenants
    with sync_engine.begin() as conn:
        _set_tenant(conn, tenant_a)
        rows = conn.execute(sa.text("SELECT tenant_id, name FROM contacts")).all()

    assert [r.name for r in rows] == ["Carla"]
    assert all(r.tenant_id == tenant_a for r in rows)
    assert tenant_b not in {r.tenant_id for r in rows}


def test_sem_contexto_de_tenant_nao_retorna_nada(
    sync_engine: sa.Engine, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Fail-closed: esquecer de abrir o contexto nao vaza, apenas nao devolve linha."""
    with sync_engine.begin() as conn:
        _set_tenant(conn, None)
        assert conn.execute(sa.text("SELECT count(*) FROM contacts")).scalar_one() == 0


def test_insercao_com_tenant_alheio_e_recusada(
    sync_engine: sa.Engine, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """O `WITH CHECK` da policy impede gravar linha carimbada com outro tenant."""
    tenant_a, tenant_b = two_tenants
    with sync_engine.begin() as conn, pytest.raises(sa.exc.ProgrammingError):
        _set_tenant(conn, tenant_a)
        conn.execute(
            sa.text(
                "INSERT INTO contacts (tenant_id, phone_e164, name) "
                "VALUES (:t, '+5511000000000', 'invasor')"
            ),
            {"t": tenant_b},
        )


def test_update_nao_alcanca_linha_de_outro_tenant(
    sync_engine: sa.Engine, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, tenant_b = two_tenants
    with sync_engine.begin() as conn:
        _set_tenant(conn, tenant_a)
        result = conn.execute(sa.text("UPDATE contacts SET name = 'hackeado'"))
        assert result.rowcount == 1  # so o proprio

    with sync_engine.begin() as conn:
        _set_tenant(conn, tenant_b)
        assert conn.execute(sa.text("SELECT name FROM contacts")).scalar_one() == "Bruno"


def test_todas_as_tabelas_com_tenant_id_tem_rls_forcada(sync_engine: sa.Engine) -> None:
    """Sem FORCE, o dono da tabela — que aqui e o usuario da app — ignora a policy."""
    with sync_engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relname = ANY(:names)"
            ),
            {"names": list(TENANT_SCOPED_TABLES)},
        ).all()

    found = {r.relname for r in rows}
    assert found == set(TENANT_SCOPED_TABLES), (
        f"tabelas ausentes no banco: {set(TENANT_SCOPED_TABLES) - found}"
    )
    sem_rls = [r.relname for r in rows if not r.relrowsecurity]
    sem_force = [r.relname for r in rows if not r.relforcerowsecurity]
    assert not sem_rls, f"sem RLS: {sem_rls}"
    assert not sem_force, f"sem FORCE RLS: {sem_force}"


def test_toda_tabela_com_coluna_tenant_id_esta_na_lista(sync_engine: sa.Engine) -> None:
    """Impede que uma tabela nova entre no schema sem RLS por esquecimento."""
    with sync_engine.connect() as conn:
        com_coluna = {
            r[0]
            for r in conn.execute(
                sa.text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND column_name = 'tenant_id'"
                )
            ).all()
        }
    assert com_coluna == set(TENANT_SCOPED_TABLES), (
        "TENANT_SCOPED_TABLES divergiu do schema: "
        f"faltando {com_coluna - set(TENANT_SCOPED_TABLES)}, "
        f"sobrando {set(TENANT_SCOPED_TABLES) - com_coluna}"
    )


# --------------------------- rede anti-overbooking ---------------------------


@pytest.fixture
def booking_fixture(sync_engine: sa.Engine) -> Iterator[dict[str, uuid.UUID]]:
    marker = uuid.uuid4().hex[:8]
    with sync_engine.begin() as conn:
        _bypass(conn)
        tenant_id = _seed_tenant(conn, f"tenant-book-{marker}")
        location_id = conn.execute(
            sa.text(
                "INSERT INTO locations (tenant_id, name, timezone, business_hours) "
                "VALUES (:t, 'Unidade', 'America/Sao_Paulo', '{}'::jsonb) RETURNING id"
            ),
            {"t": tenant_id},
        ).scalar_one()
        provider_id = conn.execute(
            sa.text(
                "INSERT INTO providers (tenant_id, location_id, name, working_hours) "
                "VALUES (:t, :l, 'Dra. Ana', '{}'::jsonb) RETURNING id"
            ),
            {"t": tenant_id, "l": location_id},
        ).scalar_one()
        service_id = conn.execute(
            sa.text(
                "INSERT INTO services (tenant_id, name, duration_minutes) "
                "VALUES (:t, 'Consulta', 30) RETURNING id"
            ),
            {"t": tenant_id},
        ).scalar_one()
        contact_id = conn.execute(
            sa.text(
                "INSERT INTO contacts (tenant_id, phone_e164, name) "
                "VALUES (:t, :p, 'Carla') RETURNING id"
            ),
            {"t": tenant_id, "p": f"+5511{marker}"},
        ).scalar_one()

    yield {
        "tenant_id": tenant_id,
        "provider_id": provider_id,
        "service_id": service_id,
        "contact_id": contact_id,
    }

    with sync_engine.begin() as conn:
        _bypass(conn)
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})


def _insert_appointment(
    conn: Connection,
    ids: dict[str, uuid.UUID],
    starts_at: datetime,
    minutes: int = 30,
    status: str = "confirmed",
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO appointments "
            "(tenant_id, contact_id, provider_id, service_id, starts_at, ends_at, status) "
            "VALUES (:t, :c, :p, :s, :start, :end, :status)"
        ),
        {
            "t": ids["tenant_id"],
            "c": ids["contact_id"],
            "p": ids["provider_id"],
            "s": ids["service_id"],
            "start": starts_at,
            "end": starts_at + timedelta(minutes=minutes),
            "status": status,
        },
    )


def test_constraint_recusa_sobreposicao_no_mesmo_profissional(
    sync_engine: sa.Engine, booking_fixture: dict[str, uuid.UUID]
) -> None:
    start = datetime(2026, 9, 9, 17, 0, tzinfo=UTC)
    with sync_engine.begin() as conn:
        _set_tenant(conn, booking_fixture["tenant_id"])
        _insert_appointment(conn, booking_fixture, start)

    with sync_engine.begin() as conn, pytest.raises(sa.exc.IntegrityError, match="appt_no_overlap"):
        _set_tenant(conn, booking_fixture["tenant_id"])
        _insert_appointment(conn, booking_fixture, start + timedelta(minutes=15))


def test_horarios_encostados_sao_permitidos(
    sync_engine: sa.Engine, booking_fixture: dict[str, uuid.UUID]
) -> None:
    """`tstzrange` e semiaberto: 17h-17h30 e 17h30-18h nao se sobrepoem."""
    start = datetime(2026, 9, 10, 17, 0, tzinfo=UTC)
    with sync_engine.begin() as conn:
        _set_tenant(conn, booking_fixture["tenant_id"])
        _insert_appointment(conn, booking_fixture, start)
        _insert_appointment(conn, booking_fixture, start + timedelta(minutes=30))

    with sync_engine.begin() as conn:
        _set_tenant(conn, booking_fixture["tenant_id"])
        total = conn.execute(sa.text("SELECT count(*) FROM appointments")).scalar_one()
    assert total == 2


def test_agendamento_cancelado_libera_o_horario(
    sync_engine: sa.Engine, booking_fixture: dict[str, uuid.UUID]
) -> None:
    """A constraint so vale para status ativos — cancelar precisa devolver o slot."""
    start = datetime(2026, 9, 11, 17, 0, tzinfo=UTC)
    with sync_engine.begin() as conn:
        _set_tenant(conn, booking_fixture["tenant_id"])
        _insert_appointment(conn, booking_fixture, start, status="cancelled")
        _insert_appointment(conn, booking_fixture, start)


def test_idempotency_key_impede_agendamento_duplicado(
    sync_engine: sa.Engine, booking_fixture: dict[str, uuid.UUID]
) -> None:
    """RNF-04: retry de `confirm_appointment` nao cria um segundo agendamento."""
    start = datetime(2026, 9, 12, 17, 0, tzinfo=UTC)
    key = f"hold-{uuid.uuid4().hex}"

    def insert(conn: Connection) -> None:
        conn.execute(
            sa.text(
                "INSERT INTO appointments (tenant_id, contact_id, provider_id, service_id, "
                "starts_at, ends_at, idempotency_key) "
                "VALUES (:t, :c, :p, :s, :start, :end, :key)"
            ),
            {
                "t": booking_fixture["tenant_id"],
                "c": booking_fixture["contact_id"],
                "p": booking_fixture["provider_id"],
                "s": booking_fixture["service_id"],
                "start": start,
                "end": start + timedelta(minutes=30),
                "key": key,
            },
        )

    with sync_engine.begin() as conn:
        _set_tenant(conn, booking_fixture["tenant_id"])
        insert(conn)

    with sync_engine.begin() as conn, pytest.raises(sa.exc.IntegrityError, match="idempotency"):
        _set_tenant(conn, booking_fixture["tenant_id"])
        insert(conn)
