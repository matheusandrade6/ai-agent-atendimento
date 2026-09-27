"""Leitura do placar versionado (secao 19.4).

O placar e um arquivo (`placar.yaml`), nao um numero calculado na hora. Ele guarda,
por cenario, **o que ja se sabe que funciona** e **quantas assercoes aquele cenario
faz**. Mudar qualquer um dos dois e uma decisao explicita, que aparece no diff.

Por que a contagem de assercoes esta no placar
-----------------------------------------------
Sem ela, "nenhum cenario caiu" e facil de conseguir apagando a assercao que incomoda:
o cenario continua listado, continua verde, e nao confere mais nada. Com a contagem
versionada, enfraquecer um cenario vira exatamente o que ele e — uma alteracao do
placar, visivel na revisao.

`known_failure` existe para defeito conhecido ficar rastreado em vez de apagado. Ele
carrega motivo obrigatorio e **tem de continuar falhando**: no dia em que passar, o
placar acusa e alguem promove a linha.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

__all__ = ["PLACAR_PATH", "Placar", "PlacarEntry", "load_placar", "load_placar_file"]

PLACAR_PATH = Path(__file__).resolve().parent / "placar.yaml"

Status = Literal["pass", "known_failure"]


class PlacarEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Status
    assercoes: int
    motivo: str = ""

    @model_validator(mode="after")
    def _motivo_obrigatorio(self) -> PlacarEntry:
        if self.status == "known_failure" and not self.motivo.strip():
            raise ValueError("known_failure exige `motivo`")
        return self


class Placar(BaseModel):
    model_config = ConfigDict(extra="forbid")

    versao: int
    atualizado_em: str
    cenarios: dict[str, PlacarEntry]


def load_placar_file(path: Path = PLACAR_PATH) -> Placar:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Placar.model_validate(data)


def load_placar(path: Path = PLACAR_PATH) -> dict[str, PlacarEntry]:
    return load_placar_file(path).cenarios
