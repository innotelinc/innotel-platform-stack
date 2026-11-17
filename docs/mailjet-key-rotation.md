# Rotating the Mailjet inbound API key pair

The inbound path (Mailjet Parse API → `https://mailjet.innotel.us/mailjet`) authenticates
with one API key pair. It lives in `cerulean/mailjet` in the edge vault as `api_key` and
`secret_key`, beside the receiver's `inbound_user`/`inbound_password`.
`scripts/vault-mailjet-env.sh` materialises it into `/run/mailjet/mailjet.env` (0600);
`mailjet-inbound.service` and `mailjet-inbound-check.service` read that file through
`ExecStartPre=-/usr/local/sbin/vault-mailjet-env`.

## The master key cannot be rotated through the API

Measured on 2026-11-16 against this account:

| Call | Result |
| --- | --- |
| `POST /v3/REST/apikey` | creates a key with `IsMaster: false`, `ACL: ""` |
| `GET /v3/REST/sender`, `/v3/REST/parseroute` with that key | `Count: 0` — the sub-key cannot read the account, so `check-mailjet-inbound` fails its sender/parseroute stage |
| `DELETE /v3/REST/apikey/<new-id>` with the master pair | `401` |
| `DELETE /v3/REST/apikey/<new-id>` with the sub-key itself | `401` |
| `PUT /v3/REST/apikey/<new-id>` `{"IsActive": false}` with the master pair | `200` — deactivation is the one write the API allows |

So a rotation cannot end with the old pair revoked from the API: the master pair is
regenerated in the Mailjet account UI (**Account → API keys → master key → Regenerate**),
and deleting a key is a UI action as well. Until that happens the exposed pair stays
valid, and the vault has to keep the master pair or the estate checks lose the account's
sender domains and parseroutes. A sub-key is not a substitute: it cannot back them.

## Procedure

1. In the Mailjet account UI, regenerate the master key pair and note the new values.
2. Write them to the vault, keeping the receiver fields:

   ```bash
   curl -s -X POST -H "X-Vault-Token: $TOKEN" \
     --data '{"data":{"api_key":"…","secret_key":"…","inbound_user":"parse","inbound_password":"…"}}' \
     https://192.168.1.71:8200/v1/cerulean/data/mailjet
   ```

3. On `mail`: `/usr/local/sbin/vault-mailjet-env`, then
   `systemctl restart mailjet-inbound` and
   `systemctl start mailjet-inbound-check.service`. The check's `mailjet` stage reads
   `/v3/REST/sender` and `/v3/REST/parseroute`; it is the proof the new pair is a master
   pair and not a sub-key.
4. Delete the old pair in the UI. The API returns `401` for that even with master
   credentials, so a rotation that stops at step 3 has left the old pair live.

A sub-key created during an attempt can only be neutralised from the API, not removed:
`PUT /v3/REST/apikey/<id>` with `{"IsActive": false}` leaves it listed but unusable.
