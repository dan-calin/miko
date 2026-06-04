"""
memory/embeddings.py — Pluggable text embeddings for Miko's semantic memory.

Picks the first available backend, in this priority:
  1. fastembed (local ONNX, fully offline)          — if the package is installed
  2. Gemini  text-embedding-004  (google-genai)      — if LLM_API_KEY is set
  3. OpenAI  text-embedding-3-small                  — if OPENAI_API_KEY is set

Local-first so that installing `fastembed` (pip install fastembed) automatically
upgrades Miko to fully-offline embeddings — better for a personal vault. Until
then it reuses whichever provider key is already in .env, so semantic memory
works with no extra install.

`embed()` returns a list of float vectors, or None when no backend is available
(callers fall back to keyword search). The active backend's `name` is recorded
alongside stored vectors so the store can re-embed if you switch backends.
"""

import importlib.util
import logging
import os

logger = logging.getLogger("miko.embed")

_backend = None          # cached chosen backend instance
_tried = False           # whether we've already attempted to pick one


class _Backend:
    name = ""
    dim = 0

    def embed(self, texts):  # -> list[list[float]]
        raise NotImplementedError


class _FastEmbed(_Backend):
    def __init__(self):
        from fastembed import TextEmbedding
        self._model = TextEmbedding("BAAI/bge-small-en-v1.5")
        self.name = "fastembed:bge-small-en-v1.5"
        self.dim = 384

    def embed(self, texts):
        return [list(map(float, v)) for v in self._model.embed(list(texts))]


class _Gemini(_Backend):
    def __init__(self, key):
        from google import genai
        from google.genai import types
        self._client = genai.Client(api_key=key)
        self._cfg = types.EmbedContentConfig(output_dimensionality=768)
        self._model = "gemini-embedding-001"
        self.name = "gemini:gemini-embedding-001"
        self.dim = 768

    def embed(self, texts):
        # Batch in one call; fall back to per-item if the batch shape is rejected.
        try:
            r = self._client.models.embed_content(
                model=self._model, contents=list(texts), config=self._cfg)
            return [list(e.values) for e in r.embeddings]
        except Exception:
            out = []
            for t in texts:
                r = self._client.models.embed_content(
                    model=self._model, contents=t, config=self._cfg)
                out.append(list(r.embeddings[0].values))
            return out


class _OpenAI(_Backend):
    def __init__(self, key, base=None):
        from openai import OpenAI
        self._client = OpenAI(api_key=key, base_url=base or None)
        self._model = "text-embedding-3-small"
        self.name = "openai:text-embedding-3-small"
        self.dim = 1536

    def embed(self, texts):
        r = self._client.embeddings.create(model=self._model, input=list(texts))
        return [list(d.embedding) for d in r.data]


def _choose():
    if importlib.util.find_spec("fastembed"):
        try:
            b = _FastEmbed()
            logger.info("embeddings: using local fastembed (offline)")
            return b
        except Exception as e:
            logger.warning(f"fastembed init failed, trying API: {e}")
    key = os.getenv("LLM_API_KEY", "")
    if key:
        try:
            return _Gemini(key)
        except Exception as e:
            logger.warning(f"gemini embeddings unavailable: {e}")
    okey = os.getenv("OPENAI_API_KEY", "")
    if okey:
        try:
            return _OpenAI(okey)
        except Exception as e:
            logger.warning(f"openai embeddings unavailable: {e}")
    return None


def get_backend():
    global _backend, _tried
    if not _tried:
        _backend = _choose()
        _tried = True
        if _backend:
            logger.info(f"embeddings backend: {_backend.name} (dim {_backend.dim})")
        else:
            logger.warning("no embeddings backend — semantic recall will fall back to keyword search")
    return _backend


def reset() -> None:
    """Forget the cached backend (e.g. after a key was added in the UI)."""
    global _backend, _tried
    _backend, _tried = None, False


def available() -> bool:
    return get_backend() is not None


def backend_name() -> str:
    b = get_backend()
    return b.name if b else ""


def embed(texts):
    """Embed one string or a list of strings → list of float vectors, or None."""
    b = get_backend()
    if not b:
        return None
    if isinstance(texts, str):
        texts = [texts]
    if not texts:
        return []
    try:
        return b.embed(texts)
    except Exception as e:
        logger.warning(f"embed failed ({b.name}): {e}")
        return None
