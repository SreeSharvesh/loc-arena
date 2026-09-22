# One minimal image shared by every LOC-Arena service, parameterized at runtime by SVC_ROLE/SVC_NAME/SVC_PORT
# Dependency-free (stdlib only) so it builds fast and stays small.
FROM python:3.12-slim
WORKDIR /app
COPY service.py /app/service.py
ENV PYTHONUNBUFFERED=1
CMD ["python", "/app/service.py"]
