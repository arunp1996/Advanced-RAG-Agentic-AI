# Kubernetes starter

These manifests are templates and need editing before use.

1. Build/push API and frontend images and replace `YOUR_REGISTRY/...`.
2. Create namespace: `kubectl apply -f deploy/k8s/namespace.yaml`
3. Create secrets securely (do not commit secrets):
   `kubectl -n advanced-rag create secret generic rag-secrets --from-literal=postgres-password='LONG_RANDOM_VALUE' --from-literal=jwt-secret='LONG_RANDOM_VALUE' --from-literal=groq-api-key='YOUR_API_KEY'`
4. Apply postgres first, then API and frontend.
6. Check: `kubectl -n advanced-rag get pods,svc,pvc`
7. Logs: `kubectl -n advanced-rag logs deploy/rag-api`
8. Port-forward: `kubectl -n advanced-rag port-forward svc/rag-frontend 8080:80`

ChromaDB uses local persistent storage here and is suitable only as a single-writer starter. For multi-replica API deployments, use an external vector database/service or a deployment designed for shared concurrent access. PostgreSQL passwords, JWT secrets and API keys must be managed with a proper secret manager in production. Use TLS ingress, NetworkPolicies, resource quotas, backups and database migrations before exposing the service.
