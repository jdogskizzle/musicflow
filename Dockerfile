FROM python:3.12-alpine
RUN pip install --no-cache-dir mutagen==1.47.0 Pillow==11.0.0 PyYAML==6.0.2
COPY musicflow.py web.py /app/
COPY static /app/static
# Shared sign-in library (the formlogin build context, see docker-compose.yaml).
COPY --from=formlogin formlogin /app/formlogin
ENV PYTHONUNBUFFERED=1
WORKDIR /app
ENTRYPOINT ["python", "-I", "/app/musicflow.py"]
