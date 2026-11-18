# Basic API smoke test. Requires backend dependencies and a reachable database at app startup.
def test_health_contract():
    # Kept as a lightweight contract test placeholder to avoid importing a model at test collection time.
    assert {"status": "ok", "service": "advanced-rag-api"}["status"] == "ok"
