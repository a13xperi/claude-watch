.PHONY: test install-test

install-test:
	pip install -e ".[test]" -q

test:
	python3 -m pytest tests/ -v
