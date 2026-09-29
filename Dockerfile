# Lean runtime image: no shell, no package manager, non-root (distroless, Python 3.11).
# Build stage uses the same Python and Debian release so compiled wheels match the runtime.
FROM python:3.11-slim-bookworm AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY kgdc ./kgdc
RUN pip install --no-cache-dir --target /deps .

FROM gcr.io/distroless/python3-debian12:nonroot
COPY --from=build /deps /deps
ENV PYTHONPATH=/deps PYTHONUNBUFFERED=1
WORKDIR /work
ENTRYPOINT ["python3", "-m", "kgdc"]
