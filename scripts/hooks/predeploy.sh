#!/usr/bin/env bash
set -euo pipefail

YELLOW='\033[0;33m'
BLUE='\033[0;34m'
GREEN='\033[0;32m'
NC='\033[0m' # No Color

echo
echo "📑 Loading environment variables from previous deployment (if available)…"
echo

if [[ -z "${AZURE_APP_CONFIG_ENDPOINT:-}" ]]; then
  echo -e "${YELLOW}⚠️  Skipping: AZURE_APP_CONFIG_ENDPOINT is not set.${NC}"
else
  echo -e "${BLUE}📦 Creating temporary virtual environment…${NC}"
  python -m venv scripts/appconfig/.venv_temp

  # make sure we can read the activate script
  chmod a+r scripts/appconfig/.venv_temp/bin/activate
  # shellcheck disable=SC1091
  source scripts/appconfig/.venv_temp/bin/activate

  echo
  echo -e "${BLUE}⬇️  Installing requirements…${NC}"
  pip install --upgrade pip
  pip install -r scripts/appconfig/requirements.txt

  echo
  echo -e "${BLUE}🚀 Running loadconfig.py…${NC}"
  python -m scripts.appconfig.loadconfig
  echo -e "${GREEN}✅ Environment variables loaded from App Configuration.${NC}"

  echo
  echo -e "${BLUE}🛢️ Defining TAG value...${NC}"
  TAG="${TAG:-$(git rev-parse --short HEAD)}"
  azd env set TAG "${TAG}"
  echo -e "${GREEN}✅ TAG name set as: ${TAG}${NC}"

  echo
  AZURE_CONTAINER_REGISTRY_NAME="$(azd env get-values \
    | grep '^AZURE_CONTAINER_REGISTRY_NAME=' \
    | cut -d '=' -f2- \
    | tr -d '"')"
  echo -e "${GREEN}🛢️ ACR Name resolved from azd: ${AZURE_CONTAINER_REGISTRY_NAME}${NC}"

  AZURE_CONTAINER_REGISTRY_ENDPOINT="$(azd env get-values \
    | grep '^AZURE_CONTAINER_REGISTRY_ENDPOINT=' \
    | cut -d '=' -f2- \
    | tr -d '"')"
  echo -e "${GREEN}🛢️ ACR Endpoint resolved from azd: ${AZURE_CONTAINER_REGISTRY_ENDPOINT}${NC}"

  echo
  echo -e "${GREEN}🔐 Logging into ACR…${NC}"
  az acr login --name "${AZURE_CONTAINER_REGISTRY_NAME}"

  AZURE_RESOURCE_GROUP="$(azd env get-values \
    | grep '^AZURE_RESOURCE_GROUP=' \
    | cut -d '=' -f2- \
    | tr -d '"')"
  echo -e "${GREEN}🛢️ Resource Group resolved from azd: ${AZURE_RESOURCE_GROUP}${NC}"

  AZURE_FRONTEND_CONTAINER_APP_NAME="$(azd env get-values \
    | grep '^AZURE_FRONTEND_CONTAINER_APP_NAME=' \
    | cut -d '=' -f2- \
    | tr -d '"')"  
  echo -e "${GREEN}🛢️ Container app name resolved from azd: ${AZURE_FRONTEND_CONTAINER_APP_NAME}${NC}"

  echo
  echo -e "${GREEN}🔐 Associating ACR…${NC}"
  az containerapp registry set \
    --name ${AZURE_FRONTEND_CONTAINER_APP_NAME} \
    --resource-group  ${AZURE_RESOURCE_GROUP} \
    --server ${AZURE_CONTAINER_REGISTRY_ENDPOINT} \
    --identity system

  echo
  echo -e "${GREEN}🛠️  Building Docker image…${NC}"
  docker build -t "${AZURE_CONTAINER_REGISTRY_ENDPOINT}/azure-gpt-rag/frontend-build:${TAG}" .

  echo
  echo -e "${GREEN}📤 Pushing image…${NC}"
  docker push "${AZURE_CONTAINER_REGISTRY_ENDPOINT}/azure-gpt-rag/frontend-build:${TAG}"

  echo
  echo -e "${BLUE}🧹 Cleaning up…${NC}"
  deactivate
  rm -rf scripts/appconfig/.venv_temp
fi