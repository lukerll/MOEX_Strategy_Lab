FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MSA_ROOT=/app
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY configs ./configs
COPY ui ./ui
COPY data/demo ./data/demo
COPY scripts ./scripts
COPY evals ./evals

RUN useradd -m agent && mkdir -p data/cache reports logs state && chown -R agent /app
USER agent

EXPOSE 8000
HEALTHCHECK CMD python -c "import httpx; httpx.get('http://localhost:8000/api/health').raise_for_status()"
CMD ["python", "-m", "moex_strategy_agent", "--host", "0.0.0.0", "--port", "8000"]
