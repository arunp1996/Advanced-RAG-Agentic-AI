
import hashlib
import uuid
import re
from pathlib import Path
from typing import List

import chromadb
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer
from pypdf import PdfReader
from docx import Document as DocxDocument

from .config import settings


# --------------------------------------------------
# ChromaDB configuration
# --------------------------------------------------

Path(settings.chroma_path).mkdir(parents=True, exist_ok=True)

chroma = chromadb.PersistentClient(path=settings.chroma_path)

collection = chroma.get_or_create_collection(
    name="advanced_rag_chunks",
    metadata={"hnsw:space": "cosine"},
)

_embedder = None


# --------------------------------------------------
# Embedding model
# --------------------------------------------------

def embedder():
    """Load the embedding model only when it is first needed."""
    global _embedder

    if _embedder is None:
        _embedder = SentenceTransformer(settings.embedding_model)

    return _embedder


# --------------------------------------------------
# File checksum
# --------------------------------------------------

def sha256(data: bytes) -> str:
    """Return the SHA-256 checksum of uploaded file bytes."""
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------
# Text extraction
# --------------------------------------------------

def extract_text(filename: str, data: bytes) -> str:
    """
    Extract text from supported PDF, TXT and DOCX files.

    Args:
        filename: Uploaded file name.
        data: Raw file contents.

    Returns:
        Extracted and cleaned text.

    Raises:
        ValueError: If the file type is unsupported or
                    no extractable text is found.
    """
    suffix = Path(filename).suffix.lower()

    if suffix == ".txt":
        text = data.decode("utf-8-sig", errors="replace")

    elif suffix == ".pdf":
        import io

        reader = PdfReader(io.BytesIO(data))

        if reader.is_encrypted:
            try:
                result = reader.decrypt("")
                if not result:
                    raise ValueError(
                        "The PDF is password-protected."
                    )
            except Exception as exc:
                raise ValueError(
                    "Unable to read encrypted PDF."
                ) from exc

        pages = []

        for page in reader.pages:
            pages.append(page.extract_text() or "")

        text = "\n".join(pages)

    elif suffix == ".docx":
        import io

        document = DocxDocument(io.BytesIO(data))

        paragraphs = [
            paragraph.text
            for paragraph in document.paragraphs
            if paragraph.text.strip()
        ]

        table_text = []

        for table in document.tables:
            for row in table.rows:
                cells = [
                    cell.text.strip()
                    for cell in row.cells
                    if cell.text.strip()
                ]

                if cells:
                    table_text.append(" | ".join(cells))

        text = "\n".join(paragraphs + table_text)

    else:
        raise ValueError(
            "Unsupported file type. Upload PDF, TXT or DOCX."
        )

    text = text.replace("\x00", "").strip()

    if not text:
        raise ValueError(
            "No extractable text found in document."
        )

    return text


# --------------------------------------------------
# Text chunking
# --------------------------------------------------

def chunk_text(
    text: str,
    chunk_chars: int = 1800,
    overlap: int = 220,
) -> List[str]:
    """
    Split text into overlapping character-based chunks.

    Overlap helps retain context between neighboring chunks.
    """
    if chunk_chars <= 0:
        raise ValueError("chunk_chars must be greater than zero.")

    if overlap < 0 or overlap >= chunk_chars:
        raise ValueError(
            "overlap must be non-negative and smaller than chunk_chars."
        )

    text = text.strip()

    if not text:
        return []

    chunks = []
    start = 0
    text_length = len(text)

    while start < text_length:
        end = min(start + chunk_chars, text_length)

        # Prefer a nearby paragraph or word boundary.
        if end < text_length:
            boundary = text.rfind("\n", start, end)

            if boundary < start + (chunk_chars // 2):
                boundary = text.rfind(" ", start, end)

            if boundary > start:
                end = boundary

        chunk = text[start:end].strip()

        if chunk:
            chunks.append(chunk)

        if end >= text_length:
            break

        next_start = end - overlap

        # Prevent an infinite loop if the boundary is too early.
        start = max(start + 1, next_start)

    return chunks


# --------------------------------------------------
# Document indexing
# --------------------------------------------------

def index_document(
    document_id: str,
    owner_user_id: int,
    filename: str,
    text: str,
) -> int:
    """
    Create embeddings and store document chunks in ChromaDB.

    Each chunk retains its document ID and owner ID so retrieval
    can enforce document-level access permissions.
    """
    chunks = chunk_text(text)

    if not chunks:
        raise ValueError(
            "No extractable text found in document."
        )

    ids = [
        f"{document_id}:{index}"
        for index in range(len(chunks))
    ]

    embeddings = embedder().encode(
        chunks,
        normalize_embeddings=True,
    ).tolist()

    metadatas = [
        {
            "document_id": document_id,
            "owner_user_id": int(owner_user_id),
            "filename": filename,
            "chunk_index": index,
        }
        for index in range(len(chunks))
    ]

    collection.upsert(
        ids=ids,
        documents=chunks,
        embeddings=embeddings,
        metadatas=metadatas,
    )

    return len(chunks)


# --------------------------------------------------
# Access-controlled retrieval
# ---------
def _search_tokens(text: str) -> list[str]:
    """Normalize text into lowercase word tokens for BM25."""
    return re.findall(r"\b\w+\b", text.lower())


def retrieve(
    query: str,
    owner_user_id: int,
    limit: int = 5,
    shared_document_ids: List[str] | None = None,
) -> list[dict]:
    """
    Hybrid retrieval using ChromaDB vector search, BM25, and RRF.

    Access rules:
      1. Retrieve the user's own documents.
      2. Also retrieve explicitly shared documents supplied by
         the trusted backend.
      3. Never retrieve another user's unshared documents.

    shared_document_ids must come from a trusted permission check.
    """
    query = query.strip()
    if not query:
        return []

    safe_limit = max(1, min(int(limit), 20))

    permitted_shared_ids = list(
        dict.fromkeys(shared_document_ids or [])
    )

    if permitted_shared_ids:
        access_filter = {
            "$or": [
                {"owner_user_id": int(owner_user_id)},
                {"document_id": {"$in": permitted_shared_ids}},
            ]
        }
    else:
        access_filter = {"owner_user_id": int(owner_user_id)}

    # Load only chunks permitted by the access filter.
    authorized = collection.get(
        where=access_filter,
        include=["documents", "metadatas"],
    )

    ids = authorized.get("ids", []) or []
    documents = authorized.get("documents", []) or []
    metadatas = authorized.get("metadatas", []) or []

    if not ids:
        return []

    # Align records by ChromaDB ID.
    records = {}
    for chunk_id, document, metadata in zip(ids, documents, metadatas):
        if document is None:
            continue
        records[chunk_id] = {
            "text": document,
            "metadata": metadata or {},
            "distance": None,
        }

    if not records:
        return []

    query_tokens = _search_tokens(query)
    corpus_ids = []
    tokenized_corpus = []

    for chunk_id, record in records.items():
        tokens = _search_tokens(record["text"])
        if tokens:
            corpus_ids.append(chunk_id)
            tokenized_corpus.append(tokens)

    # Vector ranking always runs, even when BM25 has no usable tokens.
    vector_ranked_ids = []
    vector = embedder().encode(
        [query],
        normalize_embeddings=True,
    ).tolist()[0]

    candidate_count = min(
        len(records),
        max(20, safe_limit * 4),
    )

    vector_result = collection.query(
        query_embeddings=[vector],
        n_results=candidate_count,
        where=access_filter,
        include=["documents", "metadatas", "distances"],
    )

    result_ids = (vector_result.get("ids") or [[]])[0] or []
    distances = (vector_result.get("distances") or [[]])[0] or []

    for chunk_id, distance in zip(result_ids, distances):
        if chunk_id in records:
            records[chunk_id]["distance"] = distance
            vector_ranked_ids.append(chunk_id)

    # Keyword ranking. Skip BM25 safely if there are no usable tokens.
    bm25_ranked_ids = []
    if query_tokens and tokenized_corpus:
        bm25 = BM25Okapi(tokenized_corpus)
        scores = bm25.get_scores(query_tokens)

        ranked_indices = sorted(
            range(len(scores)),
            key=lambda index: float(scores[index]),
            reverse=True,
        )

        bm25_ranked_ids = [
            corpus_ids[index]
            for index in ranked_indices
            if float(scores[index]) > 0
        ]

    # Reciprocal Rank Fusion combines rankings, not incomparable raw scores.
    rrf_scores = {}

    for ranked_ids in (vector_ranked_ids, bm25_ranked_ids):
        for rank, chunk_id in enumerate(ranked_ids, start=1):
            rrf_scores[chunk_id] = (
                rrf_scores.get(chunk_id, 0.0)
                + 1.0 / (60 + rank)
            )

    # If a query contains no searchable tokens, preserve vector-only behavior.
    if not rrf_scores:
        rrf_scores = {
            chunk_id: 1.0 / (60 + rank)
            for rank, chunk_id in enumerate(vector_ranked_ids, start=1)
        }

    merged_ids = sorted(
        rrf_scores,
        key=lambda chunk_id: rrf_scores[chunk_id],
        reverse=True,
    )

    return [
        records[chunk_id]
        for chunk_id in merged_ids[:safe_limit]
        if chunk_id in records
    ]


def delete_document_chunks(document_id: str) -> None:
    """Delete only the ChromaDB chunks belonging to one document."""
    if not document_id or not document_id.strip():
        raise ValueError("document_id is required")

    collection.delete(
        where={"document_id": document_id}
    )
