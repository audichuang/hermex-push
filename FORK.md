# audichuang/hermex-push fork

The plugin and relay that give the **Hermex Audi** app (bundle `com.audi.hermex.37hfr5v2jj`) push on the owner's
Hermes host, kept as a thin stack of small changes on top of upstream `uzairansaruzi/hermex-push`. The app side
and the system map are in [audichuang/hermex's FORK.md](https://github.com/audichuang/hermex/blob/develop/FORK.md).

## What runs where

- **Plugin** (`plugin/`): installed on the host as `~/.hermes/plugins/hermex-push`, enabled in the profiles that
  use Hermex (installing a version is a host change: back up first, restart the gateway, verify on the real host).
- **Relay** (`relay/`): Cloudflare Worker `hermex-relay-audi`, deployed with `npx cf deploy` from `relay/`
  (`cloudflare.config.ts`; `cf` still bundles through the repo's wrangler). It accepts only the Hermex Audi
  bundle id: our APNs key cannot push to upstream's app, and one undeliverable device turns every notify into a
  503 that the plugin keeps retrying. The APNs key goes in with `--secrets-file` from a private temp file.
- The host's `HERMEX_PUSH_RELAY_URL` (root `~/.hermes/.env`, which every profile falls back to) picks the relay.

## Terms

- **`fork`** is `https://github.com/audichuang/hermex-push.git`; **`origin`** is upstream (read only).
- **`develop`** (default) = upstream `main` tip + the fork's commits as a linear stack.
- **`main`** = what the host runs. Each deployment is tagged: `plugin/<version>` for an install,
  `relay/<Cloudflare version id>` for a relay deploy.
- Every fork commit carries `Fork: upstreamable` or `Fork: fork-only` (relay config, bundle ids, account ids).

## Red lines

- Branch from `fork/develop`, PR into `develop` (`-R audichuang/hermex-push`), merge by squash. Only an upstream
  rebase and a release push `develop`/`main`. One topic per PR; never mix upstreamable and fork-only changes.
- Keep changes small so rebases stay clean.
- An agent merges, installs on the host, deploys the relay, rebases, or opens an upstream PR only when asked.
- Never commit `.p8` keys, relay install keys or tokens. Account, KV, APNs key and team ids are not secrets.

## GitHub rules, merge gate

Same rulesets as hermex (`develop`: no deletion, linear history; `main`: no deletion; merge commits off).
Gate: `npm run check` in `relay/` (`relay.yml` runs it on PRs touching `relay/`) and the plugin's pytest suite.

## Release

Install or deploy from a `develop` commit, verify on the host, then record it:
`git tag -a plugin/<version> <sha> -m "<where it is installed>"` (or `relay/<version id>`) and
`git push --atomic --force-with-lease=main:<old> fork "${sha}:refs/heads/main" <tag>` (brace `${sha}` in zsh).

## Upstream rebase and upstream PRs

Same recipe as hermex with `origin/main`: back up `develop` to `backup/develop-<date>`, rebase the fork's
commits onto `origin/main`, drop ones upstream already took, run the gate, force-push with lease. Upstream PRs
cherry-pick `Fork: upstreamable` commits onto `upstream/<topic>` from `origin/main`, follow upstream's rules
(English, issue, tests, AI-use disclosure), and need the owner's approval of the text before sending.
