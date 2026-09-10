"""Interface de embeddings da base de conhecimento (secao 11.6).

Por que uma interface
----------------------
Mesma razao do `LLMProvider` (`app.agent.llm`): `ingest.py` e `retrieve.py` nao importam
o SDK de um fornecedor especifico. Isso permite trocar de provedor de embeddings sem
tocar no pipeline, e testar chunking/recuperacao sem rede.

Por que o padrao e local, nao um SDK real
------------------------------------------
A spec (11.6) descreve o pipeline — chunk, embedding, top-k por cosseno — mas nao
escolhe fornecedor, e nao ha dependencia de nenhum SDK de embeddings no projeto
(docs/DECISOES.md, D-25). `HashingEmbeddingProvider` e um bag-of-words com hash,
deterministico e sem rede: da para os testes de ingestao/recuperacao (dedup, isolamento
de tenant, corte de score) rodarem no CI sem chave de API. Nao tem qualidade semantica
de um embedding real — trocar por um fornecedor real antes de ir para producao.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.domain.models import EMBEDDING_DIM

__all__ = ["EMBEDDING_DIM", "EmbeddingProvider", "HashingEmbeddingProvider"]

_WORD_RE = re.compile(r"\w+", re.UNICODE)


class EmbeddingProvider(Protocol):
    """O que `ingest.py` e `retrieve.py` precisam de um provedor de embeddings."""

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


@dataclass(slots=True)
class HashingEmbeddingProvider:
    """Bag-of-words com hash, normalizado para norma 1 (ver docstring do modulo)."""

    dim: int = EMBEDDING_DIM

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for word in _WORD_RE.findall(text.lower()):
            digest = hashlib.sha256(word.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            return vector
        return [v / norm for v in vector]
