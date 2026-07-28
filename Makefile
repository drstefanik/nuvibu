.PHONY: install demo run worker test docker
install:
	python -m pip install -r requirements-dev.txt
demo:
	python scripts/seed_demo.py --render
run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000
worker:
	python scripts/run_worker.py
test:
	pytest
docker:
	docker compose up --build
