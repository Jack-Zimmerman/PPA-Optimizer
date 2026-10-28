# Usage:  make alu_f16.analysis            (finds alu_f16.sv or .v; top module = alu_f16)
#         make alu_f16.analysis TOP=alu CLOCK=4 PDK=sky130hd
#         make alu_f16.analysis JSON=1     (full JSON instead of the summary)
#         make alu_f16.lint                (fast elaboration check only)
#         make qualify SUITE=suites/x RTL=x.sv      (is the spec-derived suite good enough to trust?)
#         make verify SUITE=suites/x FILES="a.sv b.sv"   (gate candidates against the suite)
#         make ppa DESIGN=counter [SUITE=suites/counter]   (verify against the suite if given, then synthesis + PPA flow)
#         make rtl SPEC=examples/counter.md [FORCE=1]       (agents: resolve loose spec if needed, then write the RTL)
#         make suite SPEC=examples/counter.md [FORCE=1]     (test-author writes suites/<top> from the spec, never sees RTL)
#         make suite-improve SPEC=.. SUITE=.. RTL=..       (qualify; feed coverage/mutation gaps back to test-author)
#         make iterate SPEC=.. SUITE=.. RTL=.. CLOCK=<ns> [ITERS=5] [N=4]   (fix slack violations, then cut area)
#         make viz [DESIGN=<run>]                   (HTML report of iteration runs: every candidate, area/power/delay/latency per round)
#         make clean-cache                 (forget cached results)

PY      ?= .venv/bin/python
CLOCK   ?= 5.0
PDK     ?= sky130hd
EXTRA   ?=
# Keep macOS from idle-sleeping during long runs: a sleeping Mac suspends the Docker VM and stalls every flow.
CAFFEINATE ?= $(shell command -v caffeinate >/dev/null 2>&1 && echo caffeinate -i)
SEARCH  := . examples $(wildcard examples/*)

# $(call find,name) -> first existing <dir>/<name>.sv or .v
find = $(firstword $(foreach d,$(SEARCH),$(wildcard $(d)/$(1).sv $(d)/$(1).v)))

.PHONY: help clean-cache
help:
	@echo "make <design>.analysis   area/timing/power for <design>.sv (or .v)"
	@echo "make <design>.lint       elaboration check only"
	@echo "options: TOP=<module> CLOCK=<ns> PDK=<platform> JSON=1 EXTRA='--abc-area'"

%.analysis:
	@f="$(call find,$*)"; \
	if [ -z "$$f" ]; then echo "error: no $*.sv or $*.v found in: $(SEARCH)"; exit 1; fi; \
	echo "analyzing $$f"; \
	$(PY) -m ppa_opt.synth.openroad_flow "$$f" --top $(or $(TOP),$*) \
	    --clock-ns $(CLOCK) --pdk $(PDK) --out work/results/$*.json \
	    $(if $(JSON),--json) $(EXTRA)

%.lint:
	@f="$(call find,$*)"; \
	if [ -z "$$f" ]; then echo "error: no $*.sv or $*.v found in: $(SEARCH)"; exit 1; fi; \
	$(PY) -c "import sys; from ppa_opt.synth.openroad_flow import lint, FlowError; \
	exec('try:\n lint([sys.argv[1]], sys.argv[2]); print(\"lint clean:\", sys.argv[1])\nexcept FlowError as e:\n print(e); print(e.log_tail); raise SystemExit(1)')" \
	    "$$f" $(or $(TOP),$*)

clean-cache:
	rm -rf work/cache work/runs

# make rtl SPEC=examples/counter.md [OUT=file.sv] [FORCE=1]
#   -> spec-resolver (only if the spec has no port table) then rtl-author, via headless Claude Code
rtl:
	@if [ -z "$(SPEC)" ]; then echo 'usage: make rtl SPEC=<spec.md> [OUT=<file.sv>] [FORCE=1]'; exit 1; fi
	$(PY) -m ppa_opt.llm.devagents rtl $(SPEC) $(if $(OUT),--out $(OUT)) $(if $(FORCE),--force)
.PHONY: rtl

# make suite SPEC=examples/counter.md [OUT=suites/x] [FORCE=1]
#   -> spec-resolver if needed, then test-author (headless Claude Code); static-checks the suite
suite:
	@if [ -z "$(SPEC)" ]; then echo 'usage: make suite SPEC=<spec.md> [OUT=<suite dir>] [FORCE=1]'; exit 1; fi
	$(PY) -m ppa_opt.llm.devagents suite $(SPEC) $(if $(OUT),--out $(OUT)) $(if $(FORCE),--force)
# make suite-improve SPEC=examples/counter.md SUITE=suites/counter RTL=examples/counter.sv [ROUNDS=2]
suite-improve:
	@if [ -z "$(SPEC)" ] || [ -z "$(SUITE)" ] || [ -z "$(RTL)" ]; then echo 'usage: make suite-improve SPEC=<spec.md> SUITE=<dir> RTL=<file> [ROUNDS=2]'; exit 1; fi
	$(PY) -m ppa_opt.llm.devagents improve $(SPEC) --suite $(SUITE) --rtl $(RTL) $(if $(ROUNDS),--rounds $(ROUNDS))
.PHONY: suite suite-improve

# make iterate SPEC=examples/x.md SUITE=suites/x RTL=examples/x.sv CLOCK=4 [ITERS=5|auto] [N=4] [KNOBS=2] [BUDGET=<minutes>] [MAXLAT=1] [PIPENOW=1] [NAME=<run name>] [FRESH=1]
#   A run folder (work/iterations/<NAME or top module>) that already has a history is CONTINUED from its best design; RTL= is then not needed. FRESH=1 starts over (old folder moved aside).
#   MAXLAT>0 lets rewrites add pipeline stages (output latency changes vs the spec); tried only after cycle-exact rewrites stall
#   -> rtl-author rewrites (cycle-exact, gated by the suite) + flow-knob variants; keeps the best until timing is met, then shrinks area
iterate:
	@if [ -z "$(SPEC)" ] || [ -z "$(SUITE)" ]; then echo 'usage: make iterate SPEC=<spec.md> SUITE=<dir> [RTL=<file>] CLOCK=<ns> [ITERS=5|auto] [N=4] [KNOBS=2] [BUDGET=<min>] [MAXLAT=0] [PIPENOW=1] [NAME=<run>] [FRESH=1]  (RTL is only needed for a new run)'; exit 1; fi
	$(CAFFEINATE) env PYTHONUNBUFFERED=1 $(PY) -m ppa_opt.loop.iterate $(SPEC) --suite $(SUITE) $(if $(RTL),--rtl $(RTL)) --clock-ns $(CLOCK) $(if $(ITERS),--iters $(ITERS)) $(if $(N),--n $(N)) $(if $(KNOBS),--knobs $(KNOBS)) $(if $(BUDGET),--budget-min $(BUDGET)) $(if $(MAXLAT),--max-extra-latency $(MAXLAT)) $(if $(PIPENOW),--pipeline-now) $(if $(NAME),--name $(NAME)) $(if $(FRESH),--fresh) $(if $(PDK),--pdk $(PDK))
.PHONY: iterate

# make viz [DESIGN=<run name>]  -> work/report.html (self-contained), opened in the browser
viz:
	$(PY) -m ppa_opt.viz.report $(if $(DESIGN),--design $(DESIGN)) --open
.PHONY: viz

# make ppa DESIGN=counter [SUITE=suites/counter] [TOP=..] [CLOCK=..]
#   -> with SUITE: gate the design first (stops if it fails), then run Yosys + OpenROAD and print area/timing/power
ppa:
	@if [ -z "$(DESIGN)" ]; then echo 'usage: make ppa DESIGN=<name or path.sv> [SUITE=suites/<name>] [TOP=<module>] [CLOCK=<ns>]'; exit 1; fi
	@f="$(DESIGN)"; [ -f "$$f" ] || f="$(call find,$(DESIGN))"; \
	if [ -z "$$f" ] || [ ! -f "$$f" ]; then echo "error: no $(DESIGN) found (tried as a path, then $(DESIGN).sv/.v in: $(SEARCH))"; exit 1; fi; \
	name=$$(basename "$$f"); name=$${name%.*}; \
	if [ -n "$(SUITE)" ]; then \
	    echo "== verify $$f against $(SUITE)"; \
	    $(PY) -m ppa_opt.verify.gate verify --suite $(SUITE) "$$f" || { echo "verification failed: not running the flow"; exit 1; }; \
	else echo "warning: no SUITE given, design is NOT verified"; fi; \
	echo "== synthesis + PPA flow: $$f"; \
	$(PY) -m ppa_opt.synth.openroad_flow "$$f" --top $(or $(TOP),$$name) \
	    --clock-ns $(CLOCK) --pdk $(PDK) --out work/results/$$name.json $(if $(JSON),--json) $(EXTRA)
.PHONY: ppa

# make qualify SUITE=suites/alu_p RTL=examples/alu.sv   -> suite passes the RTL, covers the spec, kills mutants
qualify:
	@if [ -z "$(SUITE)" ] || [ -z "$(RTL)" ]; then echo 'usage: make qualify SUITE=suites/<name> RTL=<file>'; exit 1; fi
	$(PY) -m ppa_opt.verify.gate qualify --suite $(SUITE) $(RTL)
# make verify SUITE=suites/alu_p FILES="a.sv b.sv"      -> gate candidates against the suite
verify:
	@if [ -z "$(SUITE)" ] || [ -z "$(FILES)" ]; then echo 'usage: make verify SUITE=suites/<name> FILES="a.sv b.sv ..."'; exit 1; fi
	$(PY) -m ppa_opt.verify.gate verify --suite $(SUITE) $(FILES) $(if $(MAXLAT),--max-latency $(MAXLAT))
.PHONY: qualify verify

# make compare FILES="a.sv b.sv c.sv" TOP=alu CLOCK=5   -> evaluate candidates in parallel
compare:
	@if [ -z "$(FILES)" ]; then echo 'usage: make compare FILES="a.sv b.sv ..." [TOP=<module>] [CLOCK=<ns>]'; exit 1; fi
	$(CAFFEINATE) $(PY) -m ppa_opt.synth.parallel $(FILES) $(if $(TOP),--top $(TOP)) $(if $(SUITE),--verify-suite $(SUITE)) \
	    --clock-ns $(CLOCK) --pdk $(PDK) --out work/results/compare.json $(EXTRA)
.PHONY: compare
