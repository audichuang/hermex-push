# audichuang/hermex-push fork

This fork runs on the owner's Hermes host and backs the **Hermex Audi** app (bundle `com.audi.hermex.37hfr5v2jj`)
with a self-hosted relay. Upstream is `uzairansaruzi/hermex-push`. The rules match
[audichuang/hermex's FORK.md](https://github.com/audichuang/hermex/blob/develop/FORK.md).

## Terms

- **`fork`** is `https://github.com/audichuang/hermex-push.git`; **`origin`** is upstream.
- **`develop`** (default) integrates upstream `main` plus the fork's PRs. **`main`** points at what the host
  runs: the commit installed into `~/.hermes/plugins/hermex-push` and deployed as `hermex-relay-audi`.
- Every PR into `develop` is labelled **`fork-only`** (relay config, bundle ids, account ids) or
  **`upstreamable`**.


## GitHub rules

Because upstream is merged in rather than rebased onto, history is never rewritten, so GitHub enforces the
model (repository rulesets):

- **`develop: PR only`**: changes only through a PR, merged with a merge commit; no force-push, no deletion.
- **`main: release pointer`**: no force-push, no deletion. A release moves it by fast-forward to a `develop`
  commit, so `main` is always an ancestor of `develop`.

## Red lines

- Every change goes on a branch from `fork/develop` and a PR into `develop`, merged with a merge commit.
  Only an upstream sync and a release move `develop`/`main` otherwise.
- One topic per PR; never mix fork-only and upstreamable commits.
- An agent merges, deploys, installs on the host, or opens an upstream PR only when the owner asked for it.
- Never commit `.p8` keys, relay install keys or tokens. Account, KV, APNs key and team ids are not secrets.

## Merge gate

- Relay: `npm run check` in `relay/` (the `relay.yml` workflow runs it on PRs touching `relay/`).
- Plugin: its pytest suite, plus a run on the real host per the owner's host-testing notes.

## Release

- Plugin: bump `plugin/plugin.yaml`'s version, install that commit on the host, restart the gateway, verify.
- Relay: `npx cf deploy` from `relay/` (uses `cloudflare.config.ts`; the APNs key goes in with
  `--secrets-file` from a private temp file, never the repo).
- Then tag `v<plugin version>-fork.<N>` and move `main` to it in one atomic push (`git push --atomic fork <sha>:refs/heads/main <tag>`).

## Upstream sync and upstream PRs

Merge upstream `main` into `develop` through a `sync/upstream-<date>` PR. For an upstream PR, cut
`upstream/<topic>` from `origin/main`, cherry-pick the upstreamable PR's commits, follow upstream's
contribution rules (English, issue, tests, AI-use disclosure), and get the text approved before sending.
