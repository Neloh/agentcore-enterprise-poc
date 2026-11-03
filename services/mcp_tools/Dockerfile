# AgentCore Runtime requires linux/arm64 images.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    BIND_HOST=0.0.0.0 PORT=8000 \
    OTEL_PYTHON_DISTRO=aws_distro OTEL_PYTHON_CONFIGURATOR=aws_configurator

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

RUN useradd --uid 1000 --create-home appuser
USER 1000

EXPOSE 8000
# opentelemetry-instrument = ADOT auto-instrumentation (traces to CloudWatch on AgentCore).
CMD ["opentelemetry-instrument", "python", "server.py"]
