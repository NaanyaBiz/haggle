# /hassfest

Validate the integration against Home Assistant's hassfest rules.

## What this does

hassfest is the Home Assistant tool that validates custom integrations. It runs in CI via the official `home-assistant/actions/hassfest@master` GitHub Action. There are two ways to run it locally:

### Option 1: Docker (recommended for ad-hoc checks)

```bash
docker run --rm \
  -v "$(pwd)/custom_components:/github/workspace/custom_components:ro" \
  ghcr.io/home-assistant/home-assistant:dev \
  python -m script.hassfest --action validate \
  --integration-path /github/workspace/custom_components/haggle
```

### Option 2: Check CI instead

```bash
gh run list --branch $(git rev-parse --abbrev-ref HEAD) --workflow hassfest.yml --limit 5
```

Then follow the link to the failing check for details.

## Common hassfest failures

- `manifest.json` missing a required field (check docs at developers.home-assistant.io)
- `strings.json` keys not matching `translations/en.json`
- `config_flow` key true but no `config_flow.py` class
- Version not semver
- `codeowners` not a list of GitHub handles starting with `@`

## When to run

Run after any change to `manifest.json`, `strings.json`, `translations/en.json`, or `config_flow.py`.
