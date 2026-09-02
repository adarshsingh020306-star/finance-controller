PY ?= python
export PYTHONPATH := src

.PHONY: all run setup data reconcile test clean

all: run                ## default: full end-to-end pipeline

run:                    ## generate data, reconcile, report
	./run.sh

setup:                  ## install dependencies into the current interpreter
	$(PY) -m pip install -r requirements.txt

data:                   ## regenerate the synthetic dataset and ground truth
	$(PY) -m reconciler.generate_data --out data

reconcile:              ## reconcile and report against existing data
	$(PY) -m reconciler.report --out reports/results.md

test:                   ## run the test suite
	$(PY) -m unittest discover -s tests -v

clean:                  ## remove generated artefacts and caches
	rm -rf reports/results.md .cache __pycache__ src/reconciler/__pycache__ tests/__pycache__
