FROM python:3.14-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git jq \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

# Supervisor builds this Dockerfile with only this addon folder as context, so
# the zenproxy package (which lives at the repo root, outside this folder)
# can't be COPYed directly -- it's fetched fresh from the same repo instead.
ARG ZENPROXY_REF=main
RUN git clone --depth 1 --branch "${ZENPROXY_REF}" \
    https://github.com/jurriaanpro/zenproxy.git /app

WORKDIR /app
RUN uv sync --frozen --no-dev

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
