"""Chunking da base de conhecimento (secao 11.6): tamanho, overlap e casos vazios.

`ingest_document` em si depende de Postgres (le e escreve `knowledge_documents` /
`knowledge_chunks`) e tem teste proprio em `tests/integration/test_knowledge.py`. Aqui
so a parte pura: `chunk_text`.
"""

from __future__ import annotations

import pytest

from app.knowledge.ingest import CHUNK_OVERLAP_TOKENS, CHUNK_TOKENS, chunk_text


def _palavras(n: int) -> str:
    return " ".join(f"palavra{i}" for i in range(n))


def test_constantes_batem_com_a_secao_11_6() -> None:
    assert CHUNK_TOKENS == 500
    assert CHUNK_OVERLAP_TOKENS == 80


def test_conteudo_vazio_nao_gera_chunk() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n\t  ") == []


def test_conteudo_menor_que_um_chunk_gera_um_unico_chunk_igual_ao_original() -> None:
    content = _palavras(100)
    assert chunk_text(content, chunk_tokens=500, overlap_tokens=80) == [content]


def test_chunk_preserva_o_texto_original_entre_as_palavras() -> None:
    """O limite do chunk cai na fronteira da palavra, sem normalizar espacos/linhas."""
    content = "primeira linha\ncom quebra\n\ne   espacos   largos"
    assert chunk_text(content, chunk_tokens=500, overlap_tokens=80) == [content]


def test_conteudo_maior_gera_varios_chunks_com_overlap_correto() -> None:
    content = _palavras(600)
    chunks = chunk_text(content, chunk_tokens=500, overlap_tokens=80)

    assert len(chunks) == 2
    # As ultimas 80 palavras do primeiro chunk sao as primeiras 80 do segundo.
    assert chunks[0].split()[-80:] == chunks[1].split()[:80]
    # Juntos, cobrem a palavra inicial e a final sem pular nenhuma.
    assert chunks[0].split()[0] == "palavra0"
    assert chunks[1].split()[-1] == "palavra599"


def test_overlap_igual_ou_maior_que_o_chunk_e_invalido() -> None:
    with pytest.raises(ValueError):
        chunk_text(_palavras(10), chunk_tokens=80, overlap_tokens=80)
    with pytest.raises(ValueError):
        chunk_text(_palavras(10), chunk_tokens=80, overlap_tokens=100)
