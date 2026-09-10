"""`HashingEmbeddingProvider`: placeholder local ate um vendor real (docs/DECISOES.md, D-25).

Nao testa qualidade semantica — so as garantias que `ingest.py`/`retrieve.py` exigem de
qualquer `EmbeddingProvider`: dimensao estavel, determinismo, e textos parecidos mais
proximos entre si do que textos diferentes (o suficiente para o corte de score da 11.6
ter algo sensato para cortar nos testes de integracao).
"""

from __future__ import annotations

import math

import pytest

from app.knowledge.embeddings import EMBEDDING_DIM, HashingEmbeddingProvider


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


async def test_dimensao_bate_com_a_coluna_pgvector() -> None:
    provider = HashingEmbeddingProvider()
    [vector] = await provider.embed(["ola mundo"])
    assert len(vector) == EMBEDDING_DIM


async def test_e_deterministico() -> None:
    provider = HashingEmbeddingProvider()
    [a] = await provider.embed(["horario de funcionamento"])
    [b] = await provider.embed(["horario de funcionamento"])
    assert a == b


async def test_vetor_e_normalizado() -> None:
    provider = HashingEmbeddingProvider()
    [vector] = await provider.embed(["qualquer texto com algumas palavras"])
    norma = math.sqrt(sum(v * v for v in vector))
    assert norma == pytest.approx(1.0)


async def test_texto_vazio_gera_vetor_zero_sem_quebrar() -> None:
    provider = HashingEmbeddingProvider()
    [vector] = await provider.embed([""])
    assert vector == [0.0] * EMBEDDING_DIM


async def test_textos_parecidos_sao_mais_proximos_que_textos_diferentes() -> None:
    provider = HashingEmbeddingProvider()
    base, parecido, diferente = await provider.embed(
        [
            "qual o horario de funcionamento da clinica",
            "qual o horario que a clinica funciona",
            "o gato precisa de vacina antirrabica todo ano",
        ]
    )

    sim_parecido = _cosine(list(base), list(parecido))
    sim_diferente = _cosine(list(base), list(diferente))
    assert sim_parecido > sim_diferente


async def test_embed_processa_lista_na_mesma_ordem() -> None:
    provider = HashingEmbeddingProvider()
    textos = ["primeiro texto", "segundo texto", "terceiro texto"]
    vetores = await provider.embed(textos)

    for texto, vetor in zip(textos, vetores, strict=True):
        [esperado] = await provider.embed([texto])
        assert list(vetor) == list(esperado)
