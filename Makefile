.PHONY: install test web-build up down demo load-demo smoke compose

install:
	pip install -r requirements-dev.txt
	cd apps/web && npm ci

test:
	pytest

web-build:
	cd apps/web && npm run build

up:
	./scripts/dev_up.sh

down:
	./scripts/dev_down.sh

demo:
	python scripts/demo.py --reset

load-demo:
	python scripts/load_demo.py

smoke:
	python scripts/ui_smoke.py

compose:
	docker compose up -d --build
