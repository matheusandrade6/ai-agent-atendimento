"""Secret store (secao 18.1): onde mora o que nao pode morar no banco nem no YAML.

`providers.credentials_ref` e so um **ponteiro**. O refresh token do Google vive no
secret manager do deploy, cifrado em repouso por ele; a aplicacao le pelo ponteiro, em
memoria, no momento de trocar por um access token. Esta fronteira existe para que o
codigo de calendario nao saiba (nem precise saber) se o segredo veio de Vault, de um
secret manager de nuvem ou de uma variavel de ambiente.

`EnvSecretStore` e a implementacao que serve ao desenvolvimento e a deploys simples:
`credentials_ref = "env:GOOGLE_RT_CLINICA_X"` le a variavel `GOOGLE_RT_CLINICA_X`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Protocol


class SecretNotFound(Exception):
    """O ponteiro nao resolve para nenhum segredo.

    A mensagem carrega o **ponteiro**, nunca um valor.
    """


class SecretStore(Protocol):
    async def get(self, ref: str) -> str: ...


class EnvSecretStore:
    """Resolve `env:NOME` lendo a variavel de ambiente `NOME`."""

    _PREFIX = "env:"

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else os.environ

    async def get(self, ref: str) -> str:
        if not ref.startswith(self._PREFIX):
            raise SecretNotFound(f"esquema de segredo nao suportado: {ref!r}")
        name = ref.removeprefix(self._PREFIX)
        value = self._environ.get(name)
        if not value:
            raise SecretNotFound(f"segredo ausente: {ref!r}")
        return value
