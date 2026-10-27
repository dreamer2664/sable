CXX      ?= g++
CXXFLAGS ?= -O2 -std=c++17 -Wall -Wextra -Wno-unused-parameter
TARGET    = sable
PY        ?= python3

all: $(TARGET)

$(TARGET): src/sable.cpp
	$(CXX) $(CXXFLAGS) -o $@ $<

native: src/sable.cpp
	$(CXX) -O3 -march=native -std=c++17 -o $(TARGET) $<

# The scripts in this repo are committed without the execute bit: git records
# 100644 for them, so `./tests/perft.sh` and `make test` fail on a fresh clone
# with "Permission denied". Repair it here instead of hoping the workspace
# snapshot preserved it -- that is what bit the previous session.
exec-bits:
	@chmod +x tests/*.sh tools/*.sh tools/*.py 2>/dev/null || true
	@echo "exec bits set on tests/ and tools/"

deps:
	$(PY) -m pip install -q -r requirements.txt

# Fail with one clear sentence instead of six "ModuleNotFoundError: chess" errors
# from inside the suite. python-chess is a pip install and is easy to lose.
check-deps:
	@$(PY) -c "import chess, numpy, PIL" 2>/dev/null || \
	  { echo "missing Python deps (chess/numpy/pillow). Run: make deps"; exit 1; }

test: $(TARGET) exec-bits check-deps
	$(PY) -m unittest discover -s tests -p 'test_*.py' -v

test-engine: $(TARGET) exec-bits check-deps
	./tests/perft.sh
	$(PY) tools/uci_test.py

perft: $(TARGET) exec-bits
	./tests/perft.sh

clean:
	rm -f $(TARGET)

.PHONY: all native test test-engine perft exec-bits deps check-deps clean
