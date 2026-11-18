CC ?= gcc
CLANG ?= clang
CFLAGS ?= -O2 -Wall -Wextra -D_GNU_SOURCE
BPF_FLAGS ?= -O2 -g -target bpf -I/usr/include/x86_64-linux-gnu

.PHONY: all bpf native test clean

all: bpf native

bpf: bpf/sched_latency.bpf.o

bpf/sched_latency.bpf.o: bpf/sched_latency.bpf.c
	$(CLANG) $(BPF_FLAGS) -c $< -o $@

native: elg-probe

elg-probe: src/native/latency_probe.c
	$(CC) $(CFLAGS) -o $@ $<

test: all
	/usr/bin/python3 -m py_compile src/elg/*.py
	./elg-probe --sample-ms 120 --json >/dev/null
	PYTHONPATH=src /usr/bin/python3 -m unittest discover -s tests -v

clean:
	rm -f elg-probe bpf/sched_latency.bpf.o
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
