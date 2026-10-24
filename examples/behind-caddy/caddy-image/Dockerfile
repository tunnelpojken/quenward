# Caddy with the Cloudflare DNS module, for certificates via a DNS challenge.
#   docker build -t caddy-cloudflare:local caddy-image
FROM caddy:2-builder AS build
RUN xcaddy build --with github.com/caddy-dns/cloudflare

FROM caddy:2-alpine
COPY --from=build /usr/bin/caddy /usr/bin/caddy
# The official image gives the caddy binary a file capability (to bind ports
# below 1024). With every capability dropped and no-new-privileges, a binary
# asking for one does not start at all ("exec ... operation not permitted").
# This Caddy listens on 8443 and needs none, so the capability is removed.
RUN setcap -r /usr/bin/caddy 2>/dev/null || true
