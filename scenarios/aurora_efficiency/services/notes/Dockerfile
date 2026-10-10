FROM python:3.12-slim

WORKDIR /srv

COPY notes_service.py ./

USER nobody

EXPOSE 8000

CMD ["python", "-m", "notes_service"]
