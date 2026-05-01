.PHONY: contracts-check

contracts-check:
	python scripts/validate_contracts.py
	pytest -q tests/contracts
