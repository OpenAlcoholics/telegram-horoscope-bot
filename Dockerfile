FROM ghcr.io/blindfoldedsurgery/poetry:2.0.0-pipx-3.12-bookworm

COPY [ "poetry.toml", "poetry.lock", "pyproject.toml", "./" ]

RUN poetry install --no-interaction --ansi --only=main --no-root

# We don't want the tests
COPY src/horoscopebot ./src/horoscopebot

RUN poetry install --no-interaction --ansi --only-root

ARG APP_VERSION
ENV BUILD_SHA=$APP_VERSION

CMD [ "python", "-m", "horoscopebot" ]
