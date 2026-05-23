BACKEND_PORT ?= 3001
HTTPS_PROXY_PORT ?= 3000
BACKEND_RELOAD ?= false

.PHONY: help up down status app-status

help:
	@echo "Targets:"
	@echo "  make up         Restart webapp stack (nginx + backend)"
	@echo "  make down       Stop webapp stack"
	@echo "  make status     Check webapp status"

up:
	BACKEND_PORT=$(BACKEND_PORT) HTTPS_PROXY_PORT=$(HTTPS_PROXY_PORT) \
	bash scripts/stop_with_nginx.sh
	BACKEND_PORT=$(BACKEND_PORT) HTTPS_PROXY_PORT=$(HTTPS_PROXY_PORT) BACKEND_RELOAD=$(BACKEND_RELOAD) \
	bash scripts/start_with_nginx.sh

down:
	BACKEND_PORT=$(BACKEND_PORT) HTTPS_PROXY_PORT=$(HTTPS_PROXY_PORT) \
	bash scripts/stop_with_nginx.sh

status:
	@$(MAKE) app-status

app-status:
	@echo "Webapp status: https://127.0.0.1:$(HTTPS_PROXY_PORT)/api/status"
	@curl -k -fsS https://127.0.0.1:$(HTTPS_PROXY_PORT)/api/status
	@echo ""
