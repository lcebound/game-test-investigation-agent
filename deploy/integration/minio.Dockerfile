# Build the official source release; public MinIO binary image registries are unreliable.
FROM golang:1.24.8-bookworm AS build
ENV CGO_ENABLED=0
RUN go install github.com/minio/minio@RELEASE.2025-10-15T17-29-55Z
RUN mkdir -p /runtime/tmp && chmod 1777 /runtime/tmp
FROM scratch
ENV HOME=/data TMPDIR=/tmp
COPY --from=build /runtime/ /
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /go/bin/minio /minio
ENTRYPOINT ["/minio"]
