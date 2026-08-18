"""Policy retrieval: chunk the markdown in policies/, embed it, search it.

Build the index once (needs an API key, costs one embedding call per batch):

    python -m mayday.policy_index

The index is a cache, not source. Delete it and rebuild whenever the policy
documents change — nothing detects staleness for you.

Why plain cosine similarity and not a vector database: the whole corpus is
about fifty chunks, which is roughly 150 KB of floats. A brute-force dot
product over that is microseconds, and it has no server, no schema migration
and no version skew. Chroma or pgvector start paying for themselves at the
point where the index no longer fits comfortably in memory, or where several
processes need to share and update it — neither is true here.
"""

import json
import pathlib
import re
from typing import Optional

import numpy as np

# Pinned deliberately. An embedding model that silently changed would leave a
# cached index whose vectors are no longer comparable to fresh queries, and
# nothing would surface that as an error — searches would just get worse.
EMBED_MODEL = "gemini-embedding-001"

# 768 of the model's native 3072 dimensions. Gemini embeddings are trained so
# that a truncated prefix stays meaningful, and a quarter of the storage costs
# nothing measurable in quality at this corpus size.
EMBED_DIMS = 768

_ROOT = pathlib.Path(__file__).resolve().parent.parent
POLICY_DIR = _ROOT / "policies"
VECTORS_PATH = POLICY_DIR / ".index.npy"
CHUNKS_PATH = POLICY_DIR / ".index.json"

_chunks: Optional[list[dict]] = None
_vectors: Optional[np.ndarray] = None


def _client():
    """Created on demand so importing this module never needs credentials."""
    from google import genai

    return genai.Client()


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------


def chunk_document(path: pathlib.Path) -> list[dict]:
    """Split one policy document into retrievable chunks.

    Split on level-2 headings rather than a fixed token count: these documents
    are already organised into numbered clauses, and a heading boundary is a
    topic boundary. Fixed-size windows would cut EU261-2's compensation table
    in half, and half a table retrieves as confidently as a whole one while
    being wrong.
    """
    text = path.read_text()
    lines = text.splitlines()

    title = lines[0].lstrip("# ").strip() if lines else path.stem
    body = "\n".join(lines[1:])

    parts = re.split(r"^## ", body, flags=re.MULTILINE)
    chunks = []
    for index, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue
        # Anything before the first heading is the document's preamble; its
        # first line is prose, not a section name.
        section = "Overview" if index == 0 else part.splitlines()[0].strip()
        # The document title rides along in the embedded text. Without it a
        # chunk reading "GBP 520" has no idea it is about UK261.
        chunks.append(
            {
                "doc": path.name,
                "title": title,
                "section": section,
                "text": f"{title} — {section}\n\n{part}",
            }
        )
    return chunks


def load_chunks() -> list[dict]:
    chunks = []
    for path in sorted(POLICY_DIR.glob("*.md")):
        chunks.extend(chunk_document(path))
    return chunks


# --------------------------------------------------------------------------
# embedding
# --------------------------------------------------------------------------


def _embed(texts: list[str], task_type: str) -> np.ndarray:
    """Embed a list of texts, normalized to unit length.

    task_type matters: the model embeds a question and a passage into slightly
    different places on purpose, so a query embedded as a document retrieves
    noticeably worse.
    """
    from google.genai import types

    client = _client()
    vectors = []
    # Batched because one call per chunk is fifty round-trips, and the API caps
    # how many inputs it will take at once.
    for start in range(0, len(texts), 20):
        batch = texts[start : start + 20]
        response = client.models.embed_content(
            model=EMBED_MODEL,
            contents=batch,
            config=types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=EMBED_DIMS,
            ),
        )
        vectors.extend(e.values for e in response.embeddings)

    array = np.array(vectors, dtype=np.float32)
    # Truncating to 768 dims breaks the model's unit-length guarantee, so
    # renormalize. With unit vectors, cosine similarity is just a dot product.
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    return array / np.clip(norms, 1e-12, None)


def build() -> None:
    chunks = load_chunks()
    print(f"chunking {len(set(c['doc'] for c in chunks))} documents -> {len(chunks)} chunks")
    vectors = _embed([c["text"] for c in chunks], "RETRIEVAL_DOCUMENT")
    np.save(VECTORS_PATH, vectors)
    CHUNKS_PATH.write_text(json.dumps(chunks, indent=1))
    print(f"wrote {VECTORS_PATH.name} {vectors.shape} and {CHUNKS_PATH.name}")


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------


def _ensure_loaded() -> bool:
    global _chunks, _vectors
    if _chunks is not None and _vectors is not None:
        return True
    if not VECTORS_PATH.exists() or not CHUNKS_PATH.exists():
        return False
    _vectors = np.load(VECTORS_PATH)
    _chunks = json.loads(CHUNKS_PATH.read_text())
    return True


def search(query: str, k: int = 3) -> list[dict]:
    """Top-k policy chunks for a query, most relevant first.

    Raises RuntimeError if the index has not been built.
    """
    if not _ensure_loaded():
        raise RuntimeError(
            "Policy index missing. Build it with: python -m mayday.policy_index"
        )

    query_vector = _embed([query], "RETRIEVAL_QUERY")[0]
    # Unit vectors, so this dot product is the cosine of the angle between
    # the query and every chunk at once.
    scores = _vectors @ query_vector
    top = np.argsort(-scores)[:k]
    return [
        {**_chunks[i], "score": round(float(scores[i]), 4)} for i in top
    ]


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(_ROOT / "mayday" / ".env")
    build()
