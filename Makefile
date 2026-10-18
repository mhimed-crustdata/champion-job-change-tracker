.PHONY: setup run track demo test
setup:            ## create .env from the example (edit it and add your key)
	@test -f .env || cp .env.example .env; echo "Edit .env and set CRUSTDATA_API_KEY"
run:              ## discover champion moves at CUSTOMER_DOMAINS and alert Slack
	python3 tracker.py discover
track:            ## re-check the champions in champions.csv
	python3 tracker.py track --champions champions.csv
demo:             ## same as run, but never posts to Slack
	python3 tracker.py discover --dry-run --all
test:
	python3 -m unittest discover -s tests -v
