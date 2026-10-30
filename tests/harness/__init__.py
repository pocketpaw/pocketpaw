"""Proving harnesses — programs that test an architectural premise before it ships.

A harness is NOT a unit-test suite for existing code. Each subpackage takes one
claim about how the system could work, builds the smallest thing that would prove
or disprove it, and records machine-readable evidence either way. A clean negative
result is a valid outcome.

  sites_proving/  SG-1: can a Paw Site's HTML come from a renderer built ONCE,
                  instead of the current per-site SvelteKit build?
"""
