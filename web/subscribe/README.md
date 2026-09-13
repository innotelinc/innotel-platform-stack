<!-- NOTE: this directory is the shared subscribe portal.
     One small nginx serves one page per service from pages/<service>.html,
     picking the page by Host header (see nginx.conf):

         subscribe.<service>.innotel.us  ->  pages/<service>.html
         subscribe.innotel.us (apex)     ->  pages/index.html  (all services)

     The pages are GENERATED from each service repo's own brand spec by
     scripts/sync-subscribe-pages.py — do not edit the generated pages by
     hand. Prices are never baked in: every page fetches Magnate's
     /api/plans?service=<slug> live, so the master dashboard at
     admin.magnate.innotel.us is the only place a price is set. There is no
     shared catalog: each service prices itself, and /api/plans without a
     ?service= returns only platform-level plans (today: none).

     The NPM side — one proxy host per service (plus the apex), each forwarding
     here and reusing a certificate that covers its name — is owned by
     scripts/subscribe-hosts.py. Run it with --check to see drift; it is also
     what requests a certificate when a new service's page is added.

     subscribe.zeus.innotel.us is the one exception: Zeus serves its own
     subscription page from its own app (zeus-pbx-platform maps that host to
     :3001), so pages/zeus.html is only the portal-side fallback.
-->
