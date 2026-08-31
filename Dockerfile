# Build and test rsqlx on a minimal Linux environment.
# Usage:
#   docker build -t rsqlx-test .
#   docker run --rm rsqlx-test

FROM python:3.13-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Rust
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
ENV PATH="/root/.cargo/bin:${PATH}"

# Install maturin
RUN pip install --no-cache-dir maturin

WORKDIR /app
COPY . .

RUN maturin build --release --out dist
RUN pip install --no-cache-dir dist/rsqlx-*.whl

# ------------------------------------------------------------------- runner
FROM python:3.13-slim-bookworm

RUN pip install --no-cache-dir pytest psycopg2-binary pymysql
COPY --from=builder /usr/local/lib/python3.13/site-packages/rsqlx /usr/local/lib/python3.13/site-packages/rsqlx

WORKDIR /app
COPY tests/ tests/
COPY README.md .

CMD ["python", "-m", "pytest", "tests/test_sqlite.py", "-v"]
