#!/usr/bin/env bash
set -euo pipefail
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg git unzip python3 python3-venv python3-pip build-essential
echo "Base packages installed. Install Docker Engine and the Compose plugin using the official Docker Ubuntu instructions."
echo "For Kubernetes, choose ONE path: local learning (k3s or Minikube) or a managed/production cluster."
echo "Do not install Docker Desktop and a second container runtime blindly; follow the chosen runtime's official steps."
