# Native Linux x86_64 archive is produced and tested before image assembly.
FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc
COPY runtime.tar.gz /tmp/runtime.tar.gz
RUN tar -xzf /tmp/runtime.tar.gz -C /opt \
    && rm /tmp/runtime.tar.gz \
    && groupadd --gid 10001 branchdb \
    && useradd --uid 10001 --gid 10001 --create-home branchdb \
    && install -d -m 0700 -o branchdb -g branchdb /var/lib/branchdb
COPY entrypoint.sh /opt/branchdb/entrypoint.sh
ENV PATH="/opt/branchdb/bin:${PATH}" BRANCHDB_STATE=/var/lib/branchdb
USER 10001:10001
VOLUME ["/var/lib/branchdb"]
EXPOSE 5432
STOPSIGNAL SIGINT
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s CMD ["/opt/branchdb/bin/branchdb", "--json", "status", "--check"]
ENTRYPOINT ["/opt/branchdb/entrypoint.sh"]
