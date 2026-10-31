# Build context must contain the separately verified, patched linux/amd64 binary
# named miniflux. This is a new Miniflux image, never the AI-Filter .126 image.
FROM miniflux/miniflux@sha256:cd353a6af9ce663291f405eddef26fa0d567f486b521c5d34e3fb50e41938b9a
USER 65534
COPY --chown=65534:65534 miniflux /usr/bin/miniflux
# The reviewed candidate contract uses exec-form binary entrypoint and no
# inherited arguments. The fixed upstream image instead supplies the binary as CMD.
ENTRYPOINT ["/usr/bin/miniflux"]
CMD []
