"""Pytest suite: guardrails, limits and prompt contracts, with the LLM mocked.

Deliberately separate from the six script-style test files in the repo root.
Those test different things - test_chat.py measures what the model decides and
costs real money to run; this package tests what the code decides and costs
nothing. Merging them would hide which half of the behaviour a green run
actually covers.
"""
