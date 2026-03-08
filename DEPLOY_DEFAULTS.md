# Deploy Defaults (Operator Memory)

These are the standing defaults for this repo.

- Target host alias: `hetzner-recon`
- Deploy trigger branch: `master`
- App domain: `gh-roast.deeprecon.app`
- Server app path: `/root/github-roast`
- Service name: `github-roast`
- App bind: `0.0.0.0:8011`
- LLM backend on server: `CLIProxyAPI` (not relay)
- Default CLIProxyAPI URL: `http://127.0.0.1:8317`
- Caddy config source: `/root/recon/Caddyfile` in container `recon-caddy-1`

## GitHub Actions secret convention

Use the same naming convention as `deeprecon`:

- `HETZNER_HOST`
- `HETZNER_USER`
- `SSH_PRIVATE_KEY`
- `CLIPROXY_API_KEY`
- Optional: `CLIPROXY_BASE_URL`
- Optional: `DEPLOY_GH_TOKEN`

If these defaults change, update this file and the deploy workflow together.
