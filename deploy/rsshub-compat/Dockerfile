FROM diygod/rsshub@sha256:e7cd1bd8838b91f89c2ed56b0889f3754edc6aa1f1f5dac002e2e97e1079bd54

ARG COMPAT_CONTEXT_SHA256
LABEL io.ai-filter.rsshub-compat.version="v1" \
      io.ai-filter.rsshub-compat.context-sha256="${COMPAT_CONTEXT_SHA256}" \
      io.ai-filter.rsshub-compat.base-revision="f23ada8a636fbbb5f92d8e91558cf4e8ffaca66a"

COPY . /tmp/ai-filter-rsshub-compat/
RUN node /tmp/ai-filter-rsshub-compat/patch.mjs \
      --dist /app/dist \
      --manifest /app/ai-filter-rsshub-compat.json \
      --context-root /tmp/ai-filter-rsshub-compat \
      --context-sha256 "$COMPAT_CONTEXT_SHA256" \
    && rm -r /tmp/ai-filter-rsshub-compat

# The upstream ENTRYPOINT, CMD, USER and all runtime settings are inherited unchanged.
