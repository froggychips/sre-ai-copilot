# Deployment Commands
# 1. Create Secrets
kubectl apply -f k8s/base/secrets.yaml

# 2. Deploy Infrastructure (Postgres/Redis) - assumed external or using bitnami charts
# helm install redis bitnami/redis
# helm install postgres bitnami/postgresql

# 3. Run Migrations
kubectl exec -it $(kubectl get pod -l app=sre-ai-api -o jsonpath='{.items[0].metadata.name}') -- alembic upgrade head

# 4. Apply App Manifests
kubectl apply -f k8s/base/deployment.yaml
