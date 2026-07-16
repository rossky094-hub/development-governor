# Contributing

Contributions should improve a demonstrated control failure or usability gap without
adding speculative governance layers.

1. Open an issue with a minimal reproduction and the expected deterministic decision.
2. Add or update a failing test before changing behavior.
3. Keep acceptance material independent from the implementation it judges.
4. Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` and
   `PYTHONPATH=src python3 -m development_governor demo`.
5. State the exact control boundary; do not add unmeasured savings or safety claims.

Changes that require a live paid model run need explicit Owner authorization and a
separate experiment design. Documentation-only expansion without a concrete failure
or product delta is out of scope for v0.
