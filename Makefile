# Greenroom — autonomous booking & partnership agent
# Every target is safe to re-run. Nothing here reads or writes a secret to disk.

SHELL := /bin/bash
PY_VERSION := 3.12
VENV := .venv
UV := uv

# Overridable at the command line: make deploy PROJECT_ID=foo REGION=europe-west1
PROJECT_ID ?= $(shell gcloud config get-value project 2>/dev/null)
REGION     ?= europe-west2
SERVICE    ?= greenroom
RUNTIME_SA ?= greenroom-run@$(PROJECT_ID).iam.gserviceaccount.com

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: setup
setup: ## Create the Python 3.12 venv and install all dependencies
	$(UV) python install $(PY_VERSION)
	$(UV) venv --python $(PY_VERSION) $(VENV)
	$(UV) sync --all-extras
	@echo "✓ setup complete — activate with: source $(VENV)/bin/activate"

.PHONY: test
test: ## Run the pytest suite
	$(UV) run pytest -q

.PHONY: run-local
run-local: ## Run the agent + dashboard locally on :8080 in dry-run mode
	GREENROOM_DRY_RUN=true $(UV) run uvicorn greenroom.web.main:app \
	  --host 0.0.0.0 --port 8080 --reload

.PHONY: deploy
deploy: ## Build and deploy to Cloud Run (source deploy via Cloud Build; no local Docker needed)
	@test -n "$(PROJECT_ID)" || { echo "PROJECT_ID is unset. Run: gcloud config set project <id>"; exit 1; }
	gcloud run deploy $(SERVICE) \
	  --source . \
	  --project=$(PROJECT_ID) \
	  --region=$(REGION) \
	  --service-account=$(RUNTIME_SA) \
	  --allow-unauthenticated \
	  --set-env-vars=GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=$(PROJECT_ID),GOOGLE_CLOUD_LOCATION=$(REGION),GREENROOM_DRY_RUN=true

.PHONY: lint
lint: ## Ruff check + format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

.PHONY: fmt
fmt: ## Ruff autoformat
	$(UV) run ruff format .
