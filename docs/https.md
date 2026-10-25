# HTTPS behind a reverse proxy

[← Back to the README](../README.md)

How to reach QuenWard over HTTPS from other devices on your network.

The session cookie is `Secure`, so browsers only send it over HTTPS or to
`localhost`. To use the app from another machine, put it behind a reverse proxy
with a real certificate. [`examples/behind-caddy/`](../examples/behind-caddy/) is a
complete, working setup: the app plus its own Caddy, in one compose file.

**Why its own Caddy.** The app is not something to share a proxy with a public
website. A separate Caddy, on its own port, has no route from your public sites
or tunnels to the app, and a mistake in one config cannot expose the other.

**The certificate comes from a DNS challenge**, so the server does not have to be
reachable from the internet. Caddy creates a temporary TXT record through your
DNS provider's API and Let's Encrypt checks it. The example uses Cloudflare:

1. Your domain's DNS must be on Cloudflare. Create an API token with
   **Zone → DNS → Edit** and **Zone → Zone → Read**, for that zone only.
2. Save it in a file only Caddy's user can read (never in the environment, where
   `docker inspect` shows it):

   ```bash
   mkdir -p secrets caddy/data caddy/config
   nano secrets/cloudflare_token          # paste the token, save
   sudo chown 46338:46338 secrets/cloudflare_token caddy/data caddy/config
   sudo chmod 400 secrets/cloudflare_token
   ```

3. Point the name at the server **on your local DNS only** (Pi-hole, your router),
   not in public DNS. For example `stacks.example.com → 192.168.1.10` (your server). A public
   record is not needed for the certificate, and leaving it out keeps the name
   from leading anywhere outside your network.
4. Build Caddy with the Cloudflare module, then start everything:

   ```bash
   docker build -t caddy-cloudflare:local caddy-image
   echo "DOCKER_GID=$(stat -c %g /var/run/docker.sock)" > .env
   docker compose up -d
   docker logs -f quenward-caddy     # "certificate obtained successfully"
   ```

Then open `https://stacks.example.com:8443`.

Things the example does on purpose, and that matter if you build your own:

- **`ALLOWED_ORIGINS` must list the HTTPS address** exactly as typed in the
  browser, port included. Logins and actions from any other origin are refused.
- **`FORWARDED_ALLOW_IPS` names the proxy, and only the proxy.** The app then
  takes the client's IP from `X-Forwarded-For`, which the login rate limit, the
  security alerts and the audit log use. Trusting it from anywhere else lets
  anyone pick the IP they appear to come from. The example gives Caddy a fixed
  address on a small network for this. A CIDR range works too, but keep it to
  networks only the proxy is on. Leave it unset without a proxy.
- **Caddy runs as its own user** (46338), so it can read the Cloudflare token
  and the app cannot, and the other way around for the app's secrets.
- **`admin off`**: nothing can reconfigure Caddy while it runs.
- **`setcap -r` in the Caddy image.** The official image gives the binary a
  capability to bind ports below 1024. With `cap_drop: ALL` and
  `no-new-privileges` it then refuses to start (`exec ... operation not
  permitted`). Port 8443 needs no capability.
- **`resolvers 1.1.1.1 1.0.0.1`**: Caddy checks the challenge record on public
  DNS. A local resolver that answers the name itself (Pi-hole) would never show
  the TXT record.

**Using a proxy you already have** (Caddy, Traefik, nginx) works the same way:
proxy to `app:5005` (WebSockets must be passed through, which Caddy and Traefik
do by default), set `ALLOWED_ORIGINS` to the HTTPS address, and set
`FORWARDED_ALLOW_IPS` to the proxy's address on the network it shares with the
app. Do not route it through a public tunnel (Cloudflare Tunnel, ngrok) and do
not forward its port on your router.

**If the proxy is down**, the app is still reachable over an SSH tunnel through
its `127.0.0.1` port binding:

```bash
ssh -L 5005:127.0.0.1:5005 you@server
# then open http://127.0.0.1:5005 (keep it in ALLOWED_ORIGINS for this)
```
