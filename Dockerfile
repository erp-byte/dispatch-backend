# syntax=docker/dockerfile:1.6
#
# Lambda container image for the dispatch MCP server.
#
# Why a container instead of a zip? Combined size of reportlab +
# google-api-python-client + pydantic-core + mangum easily exceeds the
# 250 MB unzipped Lambda zip limit. Container images go up to 10 GB.
#
# Base image: AWS-provided Python 3.11 runtime for Lambda.
# https://gallery.ecr.aws/lambda/python
FROM public.ecr.aws/lambda/python:3.11

# Install deps first so they cache across code edits.
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir \
        -r ${LAMBDA_TASK_ROOT}/requirements.txt \
        --target ${LAMBDA_TASK_ROOT}

# Application source. Mirrors the project layout so imports resolve the
# same way they do under `main.py` locally.
COPY lambda_app.py    ${LAMBDA_TASK_ROOT}/
COPY server           ${LAMBDA_TASK_ROOT}/server
COPY services         ${LAMBDA_TASK_ROOT}/services
COPY shared           ${LAMBDA_TASK_ROOT}/shared
COPY dispatch_ingest  ${LAMBDA_TASK_ROOT}/dispatch_ingest

# The Lambda runtime will invoke this on every request.
CMD ["lambda_app.handler"]
