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
- **`main`** = the plugin commit installed on the host, tagged `plugin/<version>`. The relay is tracked only by
  `relay/<Cloudflare version id>` tags (it deploys separately and its commit need not match the plugin's).
- Every fork commit carries `Fork: upstreamable` or `Fork: fork-only` (relay config, bundle ids, account ids).

## Red lines

- Branch from `fork/develop`, PR into `develop` (`-R audichuang/hermex-push`), merge with
  `scripts/fork/merge-pr <pr> <upstreamable|fork-only> "<English subject>"` (one squash commit, one `Fork:`
  trailer). Only an upstream rebase and a release push `develop`/`main`. One topic per PR.
- Never install the plugin or deploy the relay from a `develop` that lacks what is live: compare with the
  `plugin/*` and `relay/*` tags first (an older plugin version or a relay without the Audi bundle id is a
  silent downgrade).
- Keep changes small so rebases stay clean.
- An agent merges, installs on the host, deploys the relay, rebases, or opens an upstream PR only when asked.
- Never commit `.p8` keys, relay install keys or tokens. Account, KV, APNs key and team ids are not secrets.

## GitHub rules, merge gate

Same rulesets as hermex (`develop`: no deletion, linear history; `main`: no deletion; squash merges only,
blank default message).
Gate: `npm run check` in `relay/` (`relay.yml` runs it on PRs touching `relay/`) and the plugin's pytest suite.

## Release

Install or deploy from a `develop` commit, verify on the host, then record it:
`git tag -a plugin/<version> <sha> -m "<where it is installed>"` (or `relay/<version id>`) and
move `main` with `git push --atomic --force-with-lease=main:<old> fork "${sha}:refs/heads/main" <tag>` for a plugin
install, or push only the tag for a relay deploy (brace `${sha}` in zsh).

## Upstream rebase and upstream PRs

Same recipe as hermex's FORK.md with `origin/main`: `git fetch --multiple origin fork`, take one
`base=$(git rev-parse fork/develop)` for the backup branch, the rebase and the lease, rebase onto `origin/main`,
drop commits upstream already took, run the gate, `git push --force-with-lease=develop:${base} fork HEAD:refs/heads/develop`. Upstream PRs
cherry-pick `Fork: upstreamable` commits onto `upstream/<topic>` from `origin/main`, follow upstream's rules
(English, issue, tests, AI-use disclosure), and need the owner's approval of the text before sending.
