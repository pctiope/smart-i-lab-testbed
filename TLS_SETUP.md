# TLS termination

The stack ships without TLS. For any deployment outside the lab LAN, terminate TLS
at a reverse proxy in front of the REST API and require `mqtts://` for EMQX.

## REST API + Digital Twin via Caddy (recommended)

Caddy auto-provisions Let's Encrypt certs for public domains and self-signed certs
for local hostnames.

### 1. Add Caddy to a compose overlay

Create `compose.tls.yaml`:

```yaml
services:
  caddy:
    image: caddy:2.8-alpine
    container_name: caddy_proxy
    restart: unless-stopped
    ports:
      - "443:443"
      - "80:80"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      webapp:
        condition: service_started
      web:
        condition: service_started

  webapp:
    ports: []     # Caddy now owns 443/80 on the host
  web:
    ports: []

volumes:
  caddy_data:
  caddy_config:
```

### 2. Create `Caddyfile` from the example

```bash
cp Caddyfile.example Caddyfile
# edit DOMAIN values
```

### 3. Bring up the stack with TLS

```bash
docker compose -f compose.yaml -f compose.tls.yaml up -d
```

Caddy will request a real cert if your `DOMAIN` is publicly resolvable, or fall back
to a self-signed cert with browser warnings for `localhost`/internal hostnames.

### 4. Update Digital Twin API URL

`Smart-iLab_DigitalTwin/src/main.js` L5 currently hardcodes `http://10.158.66.30:80`.
With TLS, change to your `https://api.your-lab.example`. (Properly fixing this via
`VITE_API_URL` is audit §7.2.)

### 5. Update REST API allowed-origins

In `.env`, set `ALLOWED_ORIGINS=https://digitaltwin.your-lab.example` to match the
new HTTPS Digital Twin origin.

## EMQX TLS

EMQX 5.x supports TLS on port 8883. Mount a cert and key as a volume and set:

```yaml
emqx:
  environment:
    - EMQX_LISTENERS__SSL__DEFAULT__SSL_OPTIONS__CERTFILE=/etc/certs/server.crt
    - EMQX_LISTENERS__SSL__DEFAULT__SSL_OPTIONS__KEYFILE=/etc/certs/server.key
  volumes:
    - ./certs:/etc/certs:ro
```

Then update:
- REST API: `MQTT_IP=mqtts://emqx` in `.env`
- Python subscribers: pass `tls_set()` on the paho-mqtt client constructor
  (`ESPDevices_to_Database.py`, `Zigbee2MQTT_to_Database.py`)

## Self-signed certs for lab use

If you cannot use Let's Encrypt (no public DNS), generate a local CA + server cert
once and trust it on every lab machine:

```bash
# 1. Create a CA
openssl req -x509 -newkey rsa:4096 -days 365 -nodes \
  -keyout lab-ca.key -out lab-ca.crt -subj "/CN=Smart-iLab CA"

# 2. Issue a server cert
openssl req -new -newkey rsa:2048 -nodes \
  -keyout server.key -out server.csr -subj "/CN=*.lab.local"
openssl x509 -req -in server.csr -days 365 \
  -CA lab-ca.crt -CAkey lab-ca.key -CAcreateserial -out server.crt

# 3. Mount server.crt + server.key into Caddy or EMQX as above.
# 4. Distribute lab-ca.crt to every machine and add to system trust store.
```
