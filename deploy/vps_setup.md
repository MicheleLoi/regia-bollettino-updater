# VPS Setup — regia-bollettino-updater

Manual setup guide for the founder. No automation on the VPS is required or
expected — the updater runs on the founder's computer.

## Prerequisites

- A VPS with Nginx already serving other domains (e.g. `mhc.micheleloi.pro`).
- SSH access and sudo.
- certbot (Let's Encrypt) already installed.

## 1. Create bulletin directory

```bash
sudo mkdir -p /var/www/bulletins
sudo chown $USER:$USER /var/www/bulletins
```

Create placeholder files so Nginx does not return 404 before the first upload:

```bash
echo '{"schema_version":"1.0.0","generated_at":"1970-01-01T00:00:00Z","source_count":0,"repos":[]}' \
  > /var/www/bulletins/bulletin_ecosystem.json
echo '{"schema_version":"1.0.0","generated_at":"1970-01-01T00:00:00Z","source_count":0,"patterns":[]}' \
  > /var/www/bulletins/bulletin_patterns.json
```

## 2. Nginx location block

Add the following `location` block inside the relevant `server {}` block
(or create a new server block for a dedicated subdomain):

```nginx
location /bulletins/ {
    alias /var/www/bulletins/;
    default_type application/json;
    add_header Content-Type "application/json; charset=utf-8";
    add_header Cache-Control "max-age=3600, public";
    add_header Access-Control-Allow-Origin "*";

    # AGPL §13 compliance: expose source code URL
    # Replace <owner> with the actual GitHub owner/org after `gh repo create`.
    add_header X-Source-Code "https://github.com/<owner>/regia-bollettino-updater";
}
```

Reload Nginx:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## 3. TLS (Let's Encrypt)

If the subdomain serving bulletins does not yet have a certificate:

```bash
sudo certbot --nginx -d <your-domain>
```

If reusing an existing wildcard cert, ensure the `server_name` directive
includes the relevant domain.

## 4. Verify accessibility

From the founder's local machine:

```bash
curl -I https://<your-domain>/bulletins/bulletin_ecosystem.json
```

Expected response headers:
- `HTTP/2 200`
- `content-type: application/json; charset=utf-8`
- `cache-control: max-age=3600, public`
- `access-control-allow-origin: *`
- `x-source-code: https://github.com/<owner>/regia-bollettino-updater`

Validate JSON:

```bash
curl -s https://<your-domain>/bulletins/bulletin_ecosystem.json | python -m json.tool
```

## 5. Environment variables for `updater publish`

Set these in your shell (or in a `.env` file that you source before running):

```bash
export VPS_HOST=<your-vps-hostname-or-ip>
export VPS_USER=<your-ssh-user>
export VPS_PATH=/var/www/bulletins
export VPS_KEY_PATH=~/.ssh/id_ed25519    # path to your SSH private key
export VPS_BULLETIN_URL=https://<your-domain>/bulletins
```

## 6. First live run

```bash
updater scan
updater build
updater review
updater publish
```

## 7. AGPL §13 — operator compliance note

Because `regia-bollettino-updater` runs on the founder's computer but its
output is served over the network (HTTPS), AGPL §13 requires that users
who interact with the service can obtain the source code.

This is satisfied by:
1. The GitHub repository being **public** under AGPL-3.0.
2. The `X-Source-Code` response header pointing to the public repo URL.

After completing `gh repo create --public`, update the header value in the
Nginx config above and reload Nginx.

## 8. Backup policy

`updater publish` automatically renames existing remote bulletin files to
`bulletin_ecosystem.previous.json` and `bulletin_patterns.previous.json`
before overwriting. This preserves one generation of rollback.

Manual rollback:

```bash
ssh <user>@<host> "cp /var/www/bulletins/bulletin_ecosystem.previous.json \
  /var/www/bulletins/bulletin_ecosystem.json"
```
