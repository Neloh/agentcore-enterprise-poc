# AgentCore Runtime requires linux/arm64 images.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    BIND_HOST=0.0.0.0 A2A_PORT=9000 \
    OTEL_PYTHON_DISTRO=aws_distro OTEL_PYTHON_CONFIGURATOR=aws_configurator

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

RUN useradd --uid 1000 --create-home appuser
USER 1000

EXPOSE 9000
# ADOT auto-instrumentation: Strands' OTEL spans (model calls, tool calls, A2A
# requests) are exported to CloudWatch / X-Ray when running on AgentCore.
CMD ["opentelemetry-instrument", "python", "-m", "app.main"]
