#!/bin/bash
# Grader entrypoint: hidden tests and LLM-judged criteria -> /logs/verifier/{report.json,reward.txt}.
# If grade.py can't produce a reward, it writes none and Harbor records a verifier error.
set -u
mkdir -p /logs/verifier
/opt/grader/bin/python /tests/grade.py /tests/rubric.json > /logs/verifier/grade.log 2>&1
status=$?
cat /logs/verifier/grade.log
exit $status
