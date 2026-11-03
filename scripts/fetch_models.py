"""Download the local models JARVIS uses, once per machine.

The backend starts with HF_HUB_OFFLINE=1 (no Hugging Face network check on every start),
so a model that is not already cached cannot load. Run this after installing the
requirements:

    .venv/bin/python scripts/fetch_models.py            # the models every install needs
    .venv/bin/python scripts/fetch_models.py --voice    # also Parakeet, for voice notes (~640 MB)

Each model is independent: one that fails to download is reported and the others still
come down. Exit code 1 only when the embedder (which memory cannot work without) failed.
"""
import os
import sys
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "0"
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _step(label, fn) -> bool:
    print(f"- {label}...", flush=True)
    try:
        fn()
    except Exception as exc:              # report and carry on
        print(f"  FAILED: {exc}")
        return False
    print("  ready")
    return True


def _embedder():
    from sentence_transformers import SentenceTransformer
    from backend.brain.embeddings import EMBED_MODEL
    SentenceTransformer(EMBED_MODEL, model_kwargs={"dtype": "float32"})


def _reranker():
    from sentence_transformers import CrossEncoder
    from backend.brain.rerank import MODEL_NAME
    CrossEncoder(MODEL_NAME)


def _bm25():
    from fastembed import SparseTextEmbedding
    SparseTextEmbedding(model_name="Qdrant/bm25")


def _spacy():
    import spacy
    if not spacy.util.is_package("en_core_web_sm"):
        from spacy.cli import download
        download("en_core_web_sm")


def _parakeet():
    import subprocess
    subprocess.run([sys.executable, "-m", "backend.audio.transcriber", "--download"],
                   check=True, cwd=Path(__file__).resolve().parent.parent)


def main() -> int:
    embedder_ok = _step("embedder (thenlper/gte-small, memory and search)", _embedder)
    _step("reranker (cross-encoder, sharper recall)", _reranker)
    _step("BM25 keyword model (memory search)", _bm25)
    _step("spaCy English model (memory fact extraction)", _spacy)
    if "--voice" in sys.argv:
        _step("Parakeet (voice notes, English and French)", _parakeet)
    else:
        print("- voice notes: skipped (run with --voice to download Parakeet)")
    return 0 if embedder_ok else 1


if __name__ == "__main__":
    sys.exit(main())
