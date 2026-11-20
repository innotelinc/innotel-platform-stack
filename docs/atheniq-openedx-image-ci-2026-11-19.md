# AthenIQ Open edX image — CI build (2026-11-19)

Status (2026-11-20): the image builds, and **the Unity theme is now part of it** — the
pipeline fetches the theme and layers it inside the build, so a registry tag needs no
`-unity` follow-up. No stack runs the image today: the lab stack on `atheniq-next`
(`.120`, `next.atheniq.courses`) that used to pin it was stopped the same day and its
vhost disabled. The estate stays on `.59` with the stock
`docker.io/overhangio/openedx:22.0.2-indigo`, and no cutover is planned; see
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
4. `tutor config save --set DOCKER_IMAGE_OPENEDX=ghcr.io/innotelinc/openedx-platform:<sha>-base`;
5. `tutor images build openedx --build-arg EDX_PLATFORM_REPOSITORY=<fork> --build-arg EDX_PLATFORM_VERSION=atheniq/verawood`;
6. clones the theme repo and layers the theme `COPY` steps onto that image, producing
   `<sha>` from `<sha>-base`;
7. checks the theme directory exists inside the final image;
8. pushes `<sha>` to GHCR with `GITHUB_TOKEN` (`packages: write`).

Trigger: Actions → *Build Open edX image* → Run workflow (`edx_ref`, `tutor_version`).
Expect 40–90 minutes. The tag is the baked commit, so an image always names its own code.

## Pinning it

`ghcr.io/innotelinc/openedx-platform` is a **private** package until made public, so log in
once on the host with a token that has `read:packages`:

    docker login ghcr.io -u <user> --password-stdin <<< "$GHCR_TOKEN"

`TUTOR_ROOT` is not optional — a bare `tutor` run as root targets `/root/.local/share/tutor`,
which no container reads:

    export TUTOR_ROOT=/home/tutor/tutor-next        # the stack's own tutor root
    tutor config save --set DOCKER_IMAGE_OPENEDX=ghcr.io/innotelinc/openedx-platform:<sha>
    tutor images pull openedx
    tutor local restart lms cms lms-worker cms-worker

Verify (on whichever stack runs it — the URLs below were the lab's, and
`next.atheniq.courses` answers nothing since the lab was stopped):

    curl -s -o /dev/null -w '%{http_code}\n' https://<the stack's host>/heartbeat    # 200
    docker exec tutor_local-lms-1 ./manage.py lms shell -c \
      "from django.test import Client; c=Client(HTTP_HOST='<the stack's host>'); \
       print(c.post('/search/unstable/v0/course_list_search/', \
       data={'page_size':'120','page_index':'0'}).status_code)"                     # 200
    docker exec tutor_local-lms-1 test -d /openedx/themes/unity/lms && echo themed  # themed

Rollback (one command, no rebuild):

    tutor config save --set DOCKER_IMAGE_OPENEDX=docker.io/overhangio/openedx:22.0.2-indigo
    tutor images pull openedx && tutor local restart lms cms lms-worker cms-worker

## The theme is in this image (2026-11-20)

The pipeline clones `innotelinc/atheniq` (`contrib/openedx-theme-unity` plus the course
images from `contrib/atheniq-theme`), layers the theme's `COPY` steps onto the image
Tutor just built, and pushes the layered result under the `<sha>` tag. So the registry
tag itself carries `/openedx/themes/unity`, `/openedx/staticfiles/atheniq/` and
Studio's header assets, and a stack pins the plain tag:

    tutor config save --set DOCKER_IMAGE_OPENEDX=ghcr.io/innotelinc/openedx-platform:<sha>

The old `-unity` suffix is gone; nothing should pin it any more. Both repositories are
public, so the clone needs no token and no secret is added to the fork. The workflow
asserts the theme exists in the pushed image before it declares success.

`image/atheniq-openedx/theme-layer.Dockerfile` remains as the offline bridge: it produces
the same layer from a local `contrib/` checkout, for an image built before this change.

The page-size trap that used to be listed here is closed: `SEARCH_MAX_PAGE_SIZE` lives in the
`atheniq_search` plugin now, so it survives `tutor config save` on either stack.

## Verification, 2026-11-20

**The pipeline ran.** `gh workflow run build-openedx.yml --repo
innotelinc/openedx-platform --ref atheniq/verawood -f edx_ref=atheniq/verawood -f
tutor_version=22.0.2 -f theme_ref=main` produced run **44810632400**, which concluded
`success` in 11m48s (`20:21:34Z` → `20:33:22Z`) with all twelve steps green — including
**Fetch the AthenIQ theme**, **Layer the Unity theme onto it**, **Assert the theme is in
the image** and **Push**.

That run exists because the fork-level block cleared: until 2026-11-20 the same dispatch
was refused with `HTTP 422: Actions has been disabled for this repository` (for this
workflow and for an unrelated control workflow on `master` alike), while
`GET /repos/.../actions/permissions` reported `{"enabled": true, "allowed_actions":
"all"}` and both `PUT .../actions/permissions` and
`PUT .../actions/workflows/<id>/enable` returned success without changing it. That
combination is the owner-only switch in the fork's Actions tab, and no API call clears
it — which is why the earlier pass verified the theme layer locally instead of claiming
the workflow ran.

A duplicate dispatch queued by a retry was cancelled (`44810670443`), so only the run
above consumed a runner.

What *was* also verified, independently of GitHub, is the part the pipeline exists for —
the theme layer and its assertion — built as a real image against the same release line
(`overhangio/openedx:22.0.2-indigo`), using the workflow's own inline Dockerfile and a
theme context built exactly as its "Fetch the AthenIQ theme" step builds it:

| Path inside the image | Files |
|---|---|
| `/openedx/themes/unity/lms` | 111 (matches the theme source) |
| `/openedx/themes/unity/cms` | 19 (matches the theme source) |
| `/openedx/staticfiles/atheniq` | 227 |
| `/openedx/staticfiles/atheniq/images/courses` | 198 (matches the theme source) |
| `/openedx/staticfiles/studio/unity/atheniq` | 6 |

`test -d /openedx/themes/unity/lms && test -d /openedx/staticfiles/atheniq` printed
`theme-present`, so the "Assert the theme is in the image" step passes for this context.
That does **not** cover `tutor images build openedx` or the GHCR push, which this pass did
not change; they were last proven by run `44429631686` (2026-11-19, 15m26s, success),
which predates the theme steps in this file.
