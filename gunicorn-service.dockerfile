# syntax=docker/dockerfile:experimental

# Example docker file to build and execute a Python API service
# with gunicorn

ARG alpine_version=3.12
ARG python_version=3.8

##########################
# Docker stage for Build #
##########################
FROM python:${python_version}-alpine${alpine_version} AS build

ARG poetry_version

WORKDIR /opt/build

# Install OS dependencies
RUN apk add --no-cache --virtual .build-deps postgresql-dev make git

# Install poetry
RUN wget https://raw.githubusercontent.com/python-poetry/poetry/master/get-poetry.py \
  && POETRY_VERSION=${poetry_version} python get-poetry.py
ENV PATH="/root/.poetry/bin:$PATH"

# Build virtualenv
COPY pyproject.toml poetry.lock ./
RUN python -m venv /opt/venv \
  && source /opt/venv/bin/activate \
  && pip install --upgrade pip \
  && poetry install --no-root --no-dev


####################################################
# (Default) Docker stage for executing API Service #
####################################################
FROM python:${python_version}-alpine${alpine_version} AS api_runtime

RUN adduser -D appuser

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

RUN apk add --no-cache --virtual .runtime-deps postgresql-libs

WORKDIR /home/appuser
USER appuser

COPY src/gunicorn_config.py ./
COPY src/my_app/ ./my_app/

COPY --from=build /opt/venv/ /opt/venv/
ENV PATH="/opt/venv/bin:$PATH"

CMD [ "gunicorn", "--config", "gunicorn_config.py", "my_app.main:app" ]
