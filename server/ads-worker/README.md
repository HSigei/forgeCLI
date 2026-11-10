# Forge sponsor-line server

A Cloudflare Worker with a D1 database. It picks at most one sponsor line per install per day, counts
showings so you can bill, stops a sponsor when their paid showings run out, and signs each ad so Forge
only shows lines that came from you. Free Cloudflare plan is enough.

## One-time setup

Quickest: `npx wrangler login`, then from the forge-cli folder run `python server/ads-worker/setup.py`.
It does every step below and fills in `forgecli/ads.py`; the admin token goes to
`~/.forge/ads-admin-token.txt`. By hand (about 10 minutes):

```bash
npm install -g wrangler
wrangler login
cd server/ads-worker
wrangler d1 create forge-ads                 # paste the printed database_id into wrangler.toml
wrangler d1 execute forge-ads --remote --file=schema.sql
python -m forgecli.ads keygen                # prints a PUBLIC and a PRIVATE key
wrangler secret put AD_SIGNING_KEY           # paste the PRIVATE key
wrangler secret put ADMIN_TOKEN              # paste any long random string
wrangler deploy                              # prints https://forge-ads.<you>.workers.dev
```

Then in `forgecli/ads.py` set `AD_SERVER` to the Worker URL and `AD_PUBLIC_KEY` to the PUBLIC key,
and release a new Forge version. Until both are set, Forge never asks for ads.

## Adding a sponsor

```bash
curl -X POST https://forge-ads.<you>.workers.dev/admin/sponsors \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "content-type: application/json" \
  -d '{"name":"Acme","text":"Acme DB - Postgres that scales to zero","url":"https://acme.dev/forge","paid":10000,"starts":0,"ends":4102444800}'
```

- `text`: plain words, at most 90 characters. `url`: one plain https link.
- `paid`: number of showings bought; the sponsor stops automatically at this number.
- `starts` / `ends`: unix seconds.

See counts: `curl -H "Authorization: Bearer $ADMIN_TOKEN" https://forge-ads.<you>.workers.dev/admin/stats`
Pause one: `POST /admin/sponsors/<id>/pause` (and `/resume`).

## Kill switch

Set `ADS_ENABLED = "0"` in `wrangler.toml` and `wrangler deploy` (or change the variable in the
Cloudflare dashboard). Every Forge then gets no ad, with no Forge update needed.

## What users' Forge sends

Only `v` (Forge version) and `id` (a random install id the user can reset with `/ads reset`). The Worker
stores only a hash of the id, for the once-a-day cap. No prompts, code, paths or keys ever leave Forge.

## Keep the private key safe

Anyone holding `AD_SIGNING_KEY` can sign ads every Forge will show. It lives only as a Worker secret;
never commit it. If it leaks: run keygen again, update the secret and `AD_PUBLIC_KEY`, ship a release.
