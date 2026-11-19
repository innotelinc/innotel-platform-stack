# AthenIQ — the Unity theme, layered onto the openedx-platform CI image.
#
# WHY THIS FILE EXISTS
# --------------------
# `ghcr.io/innotelinc/openedx-platform:<tag>` is built without
# `contrib/openedx-theme-unity`, so a stack running that image has no
# `/openedx/themes/unity`. Open edX then falls back to the platform default
# theme on every render and logs, once per themed request:
#
#     Theme not found in any of the themes dirs. [Theme 'unity' not found ...]
#
# The live stack never showed this because it runs a locally built
# `docker.io/overhangio/openedx:22.0.2-indigo` that carries the theme. A cutover
# onto the registry image is therefore unthemed until the theme is in the image.
#
# The `contrib/tutor-atheniq-theme` plugin patches the Tutor build at the end of
# the `openedx` stage (theme tree, the two served skins, `collectstatic`). A full
# Tutor build re-creates every stage from `ubuntu:22.04`, which is not something a
# running stack can do inside its own memory cap. This file reproduces only the
# theme COPY steps on top of the registry image, which is what the served URLs
# need:
#
#   * `/openedx/themes/unity/`                    — the theme Django resolves;
#   * `/openedx/staticfiles/atheniq/`             — linked by `head-extra.html`;
#   * `/openedx/staticfiles/studio/unity/atheniq/`— Studio's header links.
#
# BUILD (context is `contrib/` in the atheniq repo, which holds both directories):
#
#     docker build -f theme-layer.Dockerfile \
#         -t ghcr.io/innotelinc/openedx-platform:<tag>-unity \
#         contrib/
#
# The stack then points at the new tag:
#
#     tutor config save --set DOCKER_IMAGE_OPENEDX=ghcr.io/innotelinc/openedx-platform:<tag>-unity
#
# The durable fix is to run these COPY steps inside the CI image build so the
# registry tag itself carries Unity; until then this layer is the bridge.
FROM ghcr.io/innotelinc/openedx-platform:de1f9f5

USER root
COPY --chown=app:app openedx-theme-unity/lms/ /openedx/themes/unity/lms/
COPY --chown=app:app openedx-theme-unity/cms/ /openedx/themes/unity/cms/
COPY --chown=app:app openedx-theme-unity/lms/static/atheniq/ /openedx/staticfiles/atheniq/
COPY --chown=app:app openedx-theme-unity/lms/static/images/ /openedx/staticfiles/atheniq/images/
COPY --chown=app:app atheniq-theme/lms/static/images/courses/ /openedx/staticfiles/atheniq/images/courses/
COPY --chown=app:app openedx-theme-unity/cms/static/atheniq/ /openedx/staticfiles/studio/unity/atheniq/
USER app
