# The pyswe environment: a Python 3.12 workspace with the task's starting repo in /app.
# Identical for every pyswe task (tasks/sync.sh copies it); only app/ differs.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# git for the agent; curl, nodejs, npm and procps so agent harnesses install without apt.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git procps nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# The grader's toolchain lives in its own venv, so packages the agent installs or
# removes in the system Python don't change how the task is graded.
RUN python -m venv /opt/grader && /opt/grader/bin/pip install 'pytest==9.2.0' 'anthropic==2.12.0'
# pytest for the agent, to run the repo's own tests.
RUN pip install 'pytest==9.2.0'

WORKDIR /app
COPY app/ /app/
# A commit of the starting state, so the agent (and a reviewer) can diff its changes.
RUN git init -q && git add -A \
    && git -c user.name=env -c user.email=env@localhost commit -qm "starting state"
