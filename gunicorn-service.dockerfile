# syntax=docker/dockerfile:experimental

# Example docker file for a production-ready Python API service that is
# run with gunicorn.

# Pin the versions used for our base Docker image, in order to ensure
# reproducible builds. Like all dependencies, you should regularly review
# for updated versions and bump these versions.
ARG alpine_version=3.12
ARG python_version=3.8.0

# The HTTP port that gunicorn is configured to listen on
ARG port=8000


###########################################################################
# Docker Stage for Build
# ----------------------
#
# This is used as a disposable image to get operating-system-specific
# copies of all dependencies.
FROM python:${python_version}-alpine${alpine_version} AS build

ARG poetry_version

WORKDIR /opt/build

# Install OS dependencies for building python dependencies.
#
# Adjust this to suit your needs - postgres and make are simply an example.
RUN apk add --no-cache --virtual .build-deps postgresql-dev git

# Install Poetry.
#
# Note the use of `wget` instead of `curl`, because it is builtin to Alpine
# Linux and has a dramatically smaller dependency footprint.
RUN wget https://raw.githubusercontent.com/python-poetry/poetry/master/get-poetry.py \
  && POETRY_VERSION=${poetry_version} python get-poetry.py
ENV PATH="/root/.poetry/bin:$PATH"

# Build virtualenv with Python dependencies
COPY pyproject.toml poetry.lock ./
RUN python -m venv /opt/venv \
  && source /opt/venv/bin/activate \
  && pip install --upgrade pip \
  && poetry install --no-root --no-dev


###########################################################################
# (Default) Docker stage for executing API Service
# ------------------------------------------------
#
# This creates the minimum necessary image to execute the service.
FROM python:${python_version}-alpine${alpine_version} AS api_runtime

RUN adduser -D appuser

ENV PYTHONUNBUFFERED=1
EXPOSE ${port}

# Install runtime OS dependencies.
#
# Adjust this to suit your needs - postgres is simply an example.
RUN apk add --no-cache --virtual .runtime-deps postgresql-libs

WORKDIR /home/appuser
USER appuser

# Install Python code for your application.
#
# This makes some assumptions about how you have configured gunicorn.
COPY src/gunicorn_config.py ./
COPY src/my_app/ ./my_app/

# Install runtime Python dependencies by copying from the build stage.
COPY --from=build /opt/venv/ /opt/venv/
ENV PATH="/opt/venv/bin:$PATH"

# Run the service.
#
# This makes some assumptions about how you have configured gunicorn.
CMD [ "gunicorn", "--config", "gunicorn_config.py", "my_app.main:app" ]
