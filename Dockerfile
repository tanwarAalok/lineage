# "Host it" = "run this container". Index lives on the /data volume; repos are mounted under /repos.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 lineage \
    && mkdir /data /repos && chown lineage /data /repos

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir ".[pg]"

USER lineage
# Grammars are fetched at build time so the container runs with no network access.
RUN python -c "import tree_sitter_language_pack as t; t.download(['python','java','kotlin','scala','javascript','typescript','tsx','go','rust','c','cpp','csharp','php','ruby','swift','sql','yaml','json','markdown','bash','xml','html','css','cobol','vb','pascal','fortran','perl','tsql','groovy','powershell','batch'])"

ENV LINEAGE_DB_URL=sqlite:////data/index.db \
    LINEAGE_ALLOWED_ROOTS=/repos \
    LINEAGE_USER=lineage
VOLUME ["/data"]
EXPOSE 8000

# stdio by default (docker run -i). For HTTP inside a trusted network:
#   docker run -p 127.0.0.1:8000:8000 ... lineage serve --transport streamable-http --host 0.0.0.0 --allow-unauthenticated-remote
ENTRYPOINT ["lineage"]
CMD ["serve"]
