CXX      ?= g++
CXXFLAGS ?= -O2 -std=c++17 -Wall -Wextra -Wno-unused-parameter
TARGET    = sable

all: $(TARGET)

$(TARGET): src/sable.cpp
	$(CXX) $(CXXFLAGS) -o $@ $<

native: src/sable.cpp
	$(CXX) -O3 -march=native -std=c++17 -o $(TARGET) $<

test: $(TARGET)
	./tests/perft.sh
	python3 tools/uci_test.py

perft: $(TARGET)
	./tests/perft.sh

clean:
	rm -f $(TARGET)

.PHONY: all native test perft clean
