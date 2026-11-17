# skylight-lists

CLI for interacting with **Lists** (grocery / to-do) on the Skylight family
calendar (ourskylight.com), using its unofficial REST API
(`https://app.ourskylight.com/api`).

Built against the reverse-engineered API docs:
https://gist.github.com/dknowles2/b8eab833eb23eb388c3d78999a3565f8

## Setup

```bash
./skylight_lists.py login            # email + password -> OAuth PKCE, token cached at ~/.config/skylight-cli.json (chmod 600)
# or, if you already captured a bearer token from the web app:
./skylight_lists.py token --set <TOKEN>
```

## Commands

```bash
./skylight_lists.py frames                            # frame ids (device/calendar)
./skylight_lists.py lists                             # all lists on the frame
./skylight_lists.py lists --create "Groceries"
./skylight_lists.py lists --rename "Old" "New"
./skylight_lists.py lists --delete "Groceries"

./skylight_lists.py items "Groceries"                 # items with ids
./skylight_lists.py add "Groceries" Milk Eggs --section Dairy
./skylight_lists.py check "Groceries" Milk            # name must be unique, else use id
./skylight_lists.py uncheck "Groceries" 5001
./skylight_lists.py rm "Groceries" 5001 5002
./skylight_lists.py rm "Groceries" --all-checked      # bulk clear
./skylight_lists.py move "Groceries" 5001 --after 5007
./skylight_lists.py move-section "Groceries" 5001 5002 --section Dairy

./skylight_lists.py api GET /frames/5455113/lists     # raw escape hatch for anything else
```

`--frame <id>` goes after the subcommand; omit it if you have one frame
(auto-detected and cached). `--json` on read commands gives raw JSON.
`--data '<json>'` overrides/extends request payloads.

## Notes / caveats

- API contract (verified live, Nov 2026): the API is **JSON:API-shaped** —
  resources come back as `{id, type, attributes: {label, status, section,
  ...}}` inside `{data: {list: [...]}}` envelopes. The CLI normalizes these
  (`label`→name, `status` completed/pending→checked). **Writes must send flat
  payloads** (`{"data":{...}}` is rejected): `label`/`kind`/`color` for lists,
  `label`/`section` for items, `status` for check/uncheck, `ids` for bulk
  delete, `item_ids` for bulk section. Wrong keys return 422 Rails errors.
- `api_call` must send browser-like headers (`User-Agent`, `Accept`,
  `Accept-Language`); bare urllib defaults get Cloudflare "error code: 1010"
  403 blocks.
- Login follows the real OAuth2 + PKCE flow used by the Skylight web app
  (Rails CSRF + auth code exchange); tokens auto-refresh from the cached
  refresh token. If Skylight ever adds 2FA to this flow, fall back to
  `token --set`.
- Tested end-to-end against local mocks in `tests/`: `bash tests/run_all.sh`
  runs the lists-API suite (22 tests, mock mirrors the JSON:API read/write
  shapes incl. 422 payload validation) and the full login/OAuth flow suite
  (11 tests: PKCE happy path, auto-refresh, bad-creds, 2FA re-render,
  missing-Location 302). The full write path was also round-tripped against
  the live API (create/add/check/section/bulk-delete/rename/delete).
- Historical fix (Nov 2026): `http_request(..., follow_redirects=False)` must
  return 3xx responses instead of raising — urllib's redirect handler *raises*
  `HTTPError(302)` when `redirect_request()` returns None, so the login flow's
  manual redirect-chasing (needed to catch the OAuth `code`) used to abort with
  "API error: HTTP 302". `tests/test_login.sh` guards this.
