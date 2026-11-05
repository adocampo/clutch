# Branching and release policy

Two long-lived branches, one per environment:

| Branch | Role | Version scheme | Docker tag | GitHub release |
| --- | --- | --- | --- | --- |
| `dev` (default) | Daily development, integration | `X.Y.Z.devN` | `:dev` + `X.Y.Z.devN` | prerelease |
| `master` | Production, release history only | `X.Y.Z` | `:latest` + `X.Y.Z` | release |

Short-lived `feature/*` branches branch off `dev` and merge back into `dev`.

## Day-to-day work on `dev`

```bash
git checkout dev && git pull
# ... work, commit ...
git push origin dev
```

To publish a development build, bump `pyproject.toml` to the next `.devN`,
add a `CHANGELOG.md` section, then:

```bash
git commit -am "release: v2.7.0.dev6"
git tag -a v2.7.0.dev6 -m "v2.7.0.dev6"
git push origin dev v2.7.0.dev6
```

The `docker-publish` workflow builds the tag and publishes `:dev`. Because the
version is a PEP 440 dev release, it can never touch `:latest`.

## Graduating a release to `master`

Do this when a `dev` build is ready to become the stable version:

```bash
git checkout master && git pull
git merge --no-ff dev            # keep an explicit merge commit
# set pyproject.toml to the final version (2.7.0, no .devN)
# rewrite the top CHANGELOG section for the final version
git commit -am "release: v2.7.0"
git tag -a v2.7.0 -m "v2.7.0"
git push origin master v2.7.0
```

The workflow publishes `:latest` because the version is final.

Only the final version bump happens on `master`. Never tag a final `X.Y.Z`
version from `dev`.

## Hotfixes on `master`

Fix on `master`, release it, then **merge `master` back into `dev`**:

```bash
git checkout dev && git pull
git merge master
git push origin dev
```

Never cherry-pick between the two branches. Cherry-picks duplicate commits with
different SHAs, and the duplicates are what make `CHANGELOG.md` and
`pyproject.toml` conflict on every later merge.

## Why `latest` is decided by the version, not the branch

`docker-publish.yml` derives tags with `type=pep440` and `flavor: latest=auto`,
so `latest` is applied only when the tagged version is a final release. The
branch a tag points at is irrelevant.

This is deliberate: `dev` is the default branch, and a branch-based `latest`
would publish whatever the default branch happens to contain. The version
number is the only reliable guard, which is also why the workflow is tag-driven
only and offers no manual dispatch.

## Protecting `master`

`master` is protected against force pushes and deletions. Changes should reach
it through a merge (or a PR from `dev`), not a direct commit.
