# AthenIQ Open edX image — CI build (2026-11-19)

Status (2026-11-20): the image builds, and it is pinned on the **lab stack only** —
`atheniq-next` (`.120`, `next.atheniq.courses`) runs
`ghcr.io/innotelinc/openedx-platform:de1f9f5-unity`. The estate stays on `.59` with the
stock `docker.io/overhangio/openedx:22.0.2-indigo`, and no cutover is planned; see
`atheniq-stays-on-59-2026-11-20.md`.

## Why this shape

Tutor is the deployer, not the LMS. The LMS code is `edx-platform`, which Tutor builds into
`DOCKER_IMAGE_OPENEDX`. So "run our fork" = build an image from the fork in CI, then pin it.

The stock tag `.59` runs is `docker.io/overhangio/openedx:22.0.2-indigo` (Tutor 22 =
**Verawood**), carrying `edx-platform` commit `9e67d1429d` (2026-07-29). Tutor's build recipe
clones `EDX_PLATFORM_REPOSITORY` at `EDX_PLATFORM_VERSION` (default `release/verawood.1`).

`innotelinc/openedx-platform` was forked at `master`, which is ~4 months ahead of the deployed
line. Building `master` would be a platform upgrade, not a swap. So:

- branch **`atheniq/verawood`** was created in the fork at upstream `release/verawood.1`
  = `9e67d1429d5ab49a36fb9881a26457efeee0bb89` — byte-identical to what the stock image runs.

Carry AthenIQ patches on `atheniq/verawood`. Do not ship `master` until an upgrade is planned
(migrations, MFE versions, theme compatibility).

## The pipeline

`.github/workflows/build-openedx.yml` on branch `atheniq/verawood`:

1. reclaims runner disk (~20 GB of preinstalled SDKs a full build does not need);
2. `pip install tutor==22.0.2` — the recipe must match the release line;
3. resolves the fork ref to a commit and uses its short SHA as the image tag;
4. `tutor config save --set DOCKER_IMAGE_OPENEDX=ghcr.io/innotelinc/openedx-platform:<sha>`;
5. `tutor images build openedx --build-arg EDX_PLATFORM_REPOSITORY=<fork> --build-arg EDX_PLATFORM_VERSION=atheniq/verawood`;
6. pushes to GHCR with `GITHUB_TOKEN` (`packages: write`).

Trigger: Actions → *Build Open edX image* → Run workflow (`edx_ref`, `tutor_version`).
Expect 40–90 minutes. The tag is the baked commit, so an image always names its own code.

## Pinning it

`ghcr.io/innotelinc/openedx-platform` is a **private** package until made public, so log in
once on the host with a token that has `read:packages`:

    docker login ghcr.io -u <user> --password-stdin <<< "$GHCR_TOKEN"

`TUTOR_ROOT` is not optional — a bare `tutor` run as root targets `/root/.local/share/tutor`,
which no container reads:

    export TUTOR_ROOT=/home/tutor/tutor-next        # the lab box's root
    tutor config save --set DOCKER_IMAGE_OPENEDX=ghcr.io/innotelinc/openedx-platform:<sha>
    tutor images pull openedx
    tutor local restart lms cms lms-worker cms-worker

Verify:

    curl -s -o /dev/null -w '%{http_code}\n' https://next.atheniq.courses/heartbeat    # 200
    docker exec tutor_local-lms-1 ./manage.py lms shell -c \
      "from django.test import Client; c=Client(HTTP_HOST='next.atheniq.courses'); \
       print(c.post('/search/unstable/v0/course_list_search/', \
       data={'page_size':'120','page_index':'0'}).status_code)"                  # 200

Rollback (one command, no rebuild):

    tutor config save --set DOCKER_IMAGE_OPENEDX=docker.io/overhangio/openedx:22.0.2-indigo
    tutor images pull openedx && tutor local restart lms cms lms-worker cms-worker

## The theme is not in this image

The CI build does not carry `contrib/openedx-theme-unity`, so a stack on the registry image has
no `/openedx/themes/unity` and falls back to the default theme. `image/atheniq-openedx/theme-layer.Dockerfile`
reproduces just the theme `COPY` steps on top of the registry tag and pins the result as
`<sha>-unity`; that is the tag the lab stack runs. The durable fix is to run those copies
inside the CI build, so the registry tag itself carries Unity.

The page-size trap that used to be listed here is closed: `SEARCH_MAX_PAGE_SIZE` lives in the
`atheniq_search` plugin now, so it survives `tutor config save` on either stack.
