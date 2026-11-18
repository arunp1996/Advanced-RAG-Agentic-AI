import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.rag import chunk_text

def test_chunk_text_empty():
    assert chunk_text("") == []

def test_chunk_text_keeps_content():
    chunks = chunk_text("alpha beta gamma", chunk_chars=10, overlap=2)
    assert chunks
    assert "alpha" in " ".join(chunks)
    assert "gamma" in " ".join(chunks)
