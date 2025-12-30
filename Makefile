.PHONY: venv install start stop reset clean

venv:
	python3 -m venv .venv
	. .venv/bin/activate && pip install -U pip

install:
	. .venv/bin/activate && pip install -r requirements.txt

start:
	. .venv/bin/activate && python -m streamlit run app/app.py

stop:
	@echo "Stop Streamlit with Ctrl+C in the terminal where it's running."
	@echo "If you used docker services: docker compose -f infra/docker-compose.yml down"

reset:
	rm -rf storage
	streamlit cache clear || true

clean:
	rm -rf .venv
	rm -rf __pycache__ */__pycache__ */*/__pycache__

