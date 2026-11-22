#!/bin/bash
mkdir -p /logs/verifier
if grep -q hello /app/out.txt; then echo 1; else echo 0; fi > /logs/verifier/reward.txt
