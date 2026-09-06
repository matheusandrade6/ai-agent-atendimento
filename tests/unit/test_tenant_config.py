"""Validacao da config de tenant (secao 10) e dos deltas verticais (secao 22.2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.domain.tenant_config import (
    TenantConfig,
    TenantConfigError,
    load_all,
    load_tenant_config,
)

TENANTS_DIR = Path(__file__).resolve().parents[2] / "config" / "tenants"
EXAMPLE = TENANTS_DIR / "clinica-exemplo.yaml"


def _base_config() -> dict[str, Any]:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


# ------------------------------ arquivos do repo ------------------------------


def test_exemplo_valida() -> None:
    config = load_tenant_config(EXAMPLE)
    assert config.identity.vertical == "veterinaria"
    assert config.persona.agent_name == "Bia"
    assert config.scheduling.hold_ttl_minutes == 10
    assert "subject_name" in config.intake.field_keys()


def test_template_valida_apos_preenchimento_minimo() -> None:
    """O gabarito e um esqueleto: preencher os campos obrigatorios basta para valer."""
    data = yaml.safe_load((TENANTS_DIR / "_template.yaml").read_text(encoding="utf-8"))
    data["identity"]["name"] = "Clinica Teste"
    data["identity"]["vertical"] = "medica"
    data["persona"]["agent_name"] = "Ana"
    assert TenantConfig.model_validate(data).persona.agent_name == "Ana"


def test_load_all_ignora_gabaritos() -> None:
    configs = load_all(TENANTS_DIR)
    assert "clinica-exemplo" in configs
    assert not any(slug.startswith("_") for slug in configs)


# --------------------------------- segredos ---------------------------------


def test_segredo_nao_e_resolvido_na_leitura() -> None:
    """`tenants.config` guarda o placeholder; a resolucao acontece no uso (secao 18.1)."""
    config = load_tenant_config(EXAMPLE)
    assert config.channels.whatsapp is not None
    assert config.channels.whatsapp.phone_number_id == "${WA_PHONE_NUMBER_ID}"
    assert "${WA_PHONE_NUMBER_ID}" in str(config.raw_dict())


def test_resolved_substitui_pelo_ambiente() -> None:
    config = load_tenant_config(EXAMPLE)
    resolved = config.resolved({"WA_PHONE_NUMBER_ID": "12345", "WA_WABA_ID": "999"})
    assert resolved.channels.whatsapp is not None
    assert resolved.channels.whatsapp.phone_number_id == "12345"


def test_variavel_ausente_vira_string_vazia_e_nao_o_literal() -> None:
    config = load_tenant_config(EXAMPLE)
    resolved = config.resolved({})
    assert resolved.channels.whatsapp is not None
    assert resolved.channels.whatsapp.phone_number_id == ""


# --------------------------------- validacao ---------------------------------


def test_campo_desconhecido_e_erro() -> None:
    data = _base_config()
    data["persona"]["cor_favorita"] = "azul"
    with pytest.raises(Exception, match="cor_favorita"):
        TenantConfig.model_validate(data)


def test_enum_sem_options_falha_apontando_o_campo() -> None:
    data = _base_config()
    data["intake"]["required"][1].pop("options")
    with pytest.raises(Exception, match="species"):
        TenantConfig.model_validate(data)


def test_intake_com_chave_repetida_falha() -> None:
    data = _base_config()
    data["intake"]["required"].append({"key": "species", "type": "string"})
    with pytest.raises(Exception, match="species"):
        TenantConfig.model_validate(data)


def test_trigger_sem_criterio_de_match_falha() -> None:
    data = _base_config()
    data["escalation"]["triggers"].append({"id": "vazio", "action": "escalate"})
    with pytest.raises(Exception, match="vazio"):
        TenantConfig.model_validate(data)


def test_lembrete_com_offset_positivo_falha() -> None:
    data = _base_config()
    data["scheduling"]["reminders"][0]["offset_hours"] = 24
    with pytest.raises(Exception, match="negativo"):
        TenantConfig.model_validate(data)


def test_lembrete_apontando_template_inexistente_falha() -> None:
    """Erra no onboarding, nao na vespera do primeiro lembrete (secao 14.1.4)."""
    data = _base_config()
    data["scheduling"]["reminders"][0]["template"] = "template_que_nao_existe"
    with pytest.raises(Exception, match="template_que_nao_existe"):
        TenantConfig.model_validate(data)


def test_janelas_sobrepostas_no_mesmo_dia_falham() -> None:
    data = _base_config()
    data["business_hours"]["weekly"]["mon"] = [
        {"start": "08:00", "end": "13:00"},
        {"start": "12:00", "end": "19:00"},
    ]
    with pytest.raises(Exception, match="sobrepostas"):
        TenantConfig.model_validate(data)


def test_janela_com_fim_antes_do_inicio_falha() -> None:
    data = _base_config()
    data["business_hours"]["weekly"]["sat"] = [{"start": "13:00", "end": "09:00"}]
    with pytest.raises(Exception, match="invalida"):
        TenantConfig.model_validate(data)


def test_excecao_com_closed_e_windows_falha() -> None:
    data = _base_config()
    data["business_hours"]["exceptions"] = [
        {"date": "2026-12-25", "closed": True, "windows": [{"start": "09:00", "end": "12:00"}]}
    ]
    with pytest.raises(Exception, match="mutuamente exclusivos"):
        TenantConfig.model_validate(data)


def test_widget_habilitado_sem_origem_falha() -> None:
    data = _base_config()
    data["channels"]["web"]["allowed_origins"] = []
    with pytest.raises(Exception, match="allowed_origins"):
        TenantConfig.model_validate(data)


def test_tenant_sem_canal_falha() -> None:
    data = _base_config()
    data["channels"] = {"web": {"enabled": False}}
    with pytest.raises(Exception, match="canal"):
        TenantConfig.model_validate(data)


def test_yaml_invalido_traz_o_caminho_do_arquivo(tmp_path: Path) -> None:
    bad = tmp_path / "quebrado.yaml"
    bad.write_text("identity: [\n", encoding="utf-8")
    with pytest.raises(TenantConfigError, match=r"quebrado\.yaml"):
        load_tenant_config(bad)


# ---------------------- deltas verticais (secao 22.2) ----------------------
# A regra de ouro: diferenca entre verticais e configuracao, nunca codigo.
# Se um destes exigisse mudanca de schema, o produto teria deixado de ser um so.


def test_delta_salao_de_beleza() -> None:
    data = _base_config()
    data["identity"]["vertical"] = "estetica"
    data["scheduling"]["allow_service_bundle"] = True
    data["intake"]["required"] = [
        {"key": "service", "type": "service_ref", "allow_multiple": True},
        {"key": "preferred_professional", "type": "provider_ref", "optional": True},
        {
            "key": "hair_length",
            "label": "comprimento do cabelo",
            "type": "enum",
            "options": ["curto", "medio", "longo"],
            "affects_duration": True,
        },
    ]
    data["intake"]["conditional"] = []
    config = TenantConfig.model_validate(data)
    assert config.scheduling.allow_service_bundle
    assert config.intake.required[0].allow_multiple
    assert config.intake.required[2].affects_duration


def test_delta_clinica_medica() -> None:
    data = _base_config()
    data["identity"]["vertical"] = "medica"
    data["intake"]["required"] = [
        {"key": "is_first_visit", "type": "boolean"},
        {"key": "payment_type", "type": "enum", "options": ["particular", "convenio"]},
    ]
    data["intake"]["conditional"] = [
        {
            "when": "payment_type == 'convenio'",
            "require": [
                {
                    "key": "insurance_name",
                    "type": "enum_ref",
                    "source": "accepted_insurances",
                    "on_other": "escalate",
                }
            ],
        }
    ]
    data["escalation"]["triggers"] = [
        {
            "id": "sintoma_grave",
            "match_keywords": ["dor no peito", "falta de ar", "desmaio", "sangramento"],
            "action": "escalate_immediately",
            "reply": (
                "Isso precisa de atendimento imediato. Procure um pronto-socorro ou ligue 192."
            ),
        }
    ]
    config = TenantConfig.model_validate(data)
    assert config.intake.conditional[0].require[0].on_other == "escalate"
    assert config.escalation.triggers[0].action == "escalate_immediately"


def test_delta_fisioterapeuta() -> None:
    data = _base_config()
    data["identity"]["vertical"] = "fisioterapia"
    data["scheduling"]["allow_recurring"] = True
    data["scheduling"]["recurrence_max_sessions"] = 10
    data["scheduling"]["recurrence_pattern"] = "weekly_same_slot"
    config = TenantConfig.model_validate(data)
    assert config.scheduling.recurrence_max_sessions == 10
    assert config.scheduling.recurrence_pattern == "weekly_same_slot"
