# Server deployment

Deployed on 2026-10-17 to `45.136.13.98`.
Console: https://cards.45.136.13.98.sslip.io
Release: `/opt/card-console/releases/20261024-multi-card`.
Existing local accounts and workspace data were migrated; copied sessions were invalidated.
HTTPS, anonymous access rejection, Goofish login and read-only seller order queries were verified.
The local management service was stopped. Real fulfillment remains disabled until enabled in the console.

Runtime: Ubuntu, Python 3.10+, an isolated virtual environment, systemd, and the server's existing Nginx.
Deploy the management UI and card fulfillment engine together; do not run the original AI entry point.

- Versioned source: `/opt/card-console/releases/<release>`, selected by `/opt/card-console/current`.
- Virtual environment: `/opt/card-console/venv` using `requirements-cards.txt`.
- Persistent account and business data: `/var/lib/card-console/data`.
- Each release links its `data` and legacy `.env` to `/var/lib/card-console/`.
- Service account: `card-console`, without interactive login; files under persistent storage are private.
- Service definition: `deploy/card-console.service`; configuration: `/etc/card-console.env`.
- `ADMIN_PUBLIC_URL` is the exact HTTPS origin, with no path. Only that public Host/Origin is accepted.
- HTTPS sessions use Secure, HttpOnly, SameSite=Strict cookies. HTTP redirects to HTTPS.
- Public deployment refuses the first-account setup endpoint. Initialize locally and migrate the account database.
- Only Nginx is public; the Python service stays bound to `127.0.0.1:8765`.

Before migration, stop the local management server and its owned delivery processes. Create consistent snapshots
of all SQLite databases, preserve the user workspace paths, and transfer privately over SSH. Invalidate copied
browser sessions so everyone signs in again on the server using their existing password. Never package private
data or Cookie files into the source archive. Do not run local and server delivery concurrently.

Enable and start `card-console.service`. It starts the management server on boot. Delivery stays stopped until
each user enables it in the UI; after a management service restart, users must enable their delivery again.
Platform shipping calls are never made during deployment validation. A real paid-order test is still needed
after configuring real product inventory.

Delivery copy is configurable under 运行设置 → 发货文案. The per-user workspace stores
`data/delivery-template.json` with private permissions. Keep exactly one `{card}` placeholder.
Both real fulfillment and the optional CLI chat simulation load this template before each new delivery;
atomic saves apply to the next delivery without restarting a worker. Missing configuration uses the default
Chinese greeting, tutorial/download placeholders and closing. Replace the tutorial/download placeholders
before enabling delivery. The preview uses a fictional example code and never sends a message.

The console has three explicit runtime modes: observation (reads orders without sending), chat simulation
(`mock-chat`, sends a `MOCK-TEST-` code when another account sends a new exact `已支付` message after startup),
and real paid-order fulfillment. Chat simulation uses the user's separate `data/mock-cards.db`, does not
consume real stock or mark orders shipped, and must be stopped before switching to real fulfillment.

QR login is under 运行设置 → 扫码登录闲鱼. Stop the delivery worker first. The backend calls the official
Goofish login endpoints, displays a QR image only to the authenticated workspace, and polls scan confirmation
in a bounded background thread. The user scans and confirms using the official mobile app. The browser polls
authenticated CSRF-protected endpoints; after confirmation, the backend applies the existing permanent seller
ownership check and saves Cookie privately to that user's workspace, never returning Cookie/token values.
The user then chooses and starts the desired worker mode. Manual Cookie entry remains a fallback.

Sessions expire in five minutes, one active session per user, with at most two network workers globally.
Closing the dialog cancels the attempt. Disabling an account, saving a manual Cookie, or starting a worker
cancels its pending login; application shutdown cancels all attempts. Network calls have bounded timeouts.
Expired/cancelled sessions cannot save credentials. Arbitrary redirect URLs are never followed. If the platform
requests additional verification, the user must complete it through the official site before retrying.
Dependency `qrcode==8.2` renders SVG locally; no external QR rendering service receives login contents.

On 2026-10-24, anonymous QR generation on the server returned a valid waiting QR image. A complete login
still requires the user's phone scan and confirmation; that step is not performed by deployment validation.

The confirmation handler collects login cookies directly and only bootstraps a token again if it is missing.
QR failures expose fixed stage/error identifiers in the dialog and systemd journal; raw API responses, URLs,
QR contents and credential values are never logged. A failed phone-confirmation attempt reported on 2026-10-24
could not be retrospectively diagnosed from the older generic error; the next attempt supplies these identifiers.

Manual Cookie guidance is displayed next to the Cookie input under 运行设置, including a link to Goofish,
Mac/Windows developer-tool shortcuts, the network-request filter and the exact Request Headers → Cookie
location. A copy-script button copies the fixed text `copy(document.cookie)` only, for the user to execute
in the official site's browser console. This only reads script-accessible cookies, not HttpOnly cookies;
the UI directs users to the full request-header Cookie if the result is incomplete or login fails.
The button never reads browser credentials or submits anything. Manual saving remains the existing explicit
Cookie-save action, available after stopping the worker. The guide is a static update and requires no restart.

The inventory dialog displays full card values for the authenticated workspace. Allocated cards retain an explicit
re-enable action; clicking it releases the complete order allocation and returns the cards to available stock. While
allocated, editing, deletion and selection stay disabled. For a multi-card order, re-enabling any one card releases
the complete batch so a partial allocation cannot remain.

Inventory management supports single deletion, checkbox selection and selecting all unallocated stock for
the current product. Selecting all covers rows beyond the 500 visible records, including disabled stock.
The browser confirms the scope before submitting. Deletion is transactional and scoped to the signed-in
seller/product; a mixed selected batch containing an allocated or missing row fails without partial changes.
Full-product deletion skips every allocated row. Delivery records and their deduplication remain intact.

`cards.deleted` is migrated on store initialization. Removed unused entries are hidden from lists/counts
and cannot be reserved, edited or revealed. They are also disabled for compatibility with older workers.
IDs and uniqueness are retained; explicitly reimporting the same unused secret restores it, allowing recovery
without reusing an ID for a different card. No inventory is removed during deployment or UI validation.

Multi-card fulfillment reads the authoritative seller-order field `priceVO.buyNum`, verified read-only on the
server on 2026-10-24. Only positive integers from 1 through 100 are accepted; missing/invalid quantities are
logged and skipped instead of assuming one. Payment revalidation and pre-shipment checks include quantity.

The `delivery_cards` mapping stores every allocated card under one delivery/order. Existing single-card
records migrate idempotently. One transaction reserves all required stock or none; secondary cards are locked
against further allocation, editing, deletion and reimport. A quantity change fails revalidation. One message
uses the existing template, replacing `{card}` with a numbered list for quantities above one; a single card
keeps its original format. Messages over the application's 20,000-byte limit are held before allocation.
Lost acknowledgments keep the entire batch uncertain; no automatic partial resend or code reuse is allowed.
Only a confirmed complete message queues one platform shipment job with the original quantity.

The console displays per-order card count and counts sent cards rather than orders. Chat simulation accepts
`已支付 2` (or another quantity up to 100) to test a multi-card message in its separate mock database; it never
consumes real inventory or confirms platform shipment. Real paid-order quantity-2 validation still requires
a real buyer order after enabling the service.

IMPORTANT ROLLBACK BOUNDARY: after the first multi-card allocation, never downgrade to a release that does
not understand `delivery_cards`. Such a release only knows the first card and can wrongly reuse the others.
Use a forward fix or a compatible rollback release. Never restore a stale business database to work around
this boundary; doing so loses delivery history. Before any multi-card allocation, release rollback is safe.

The Nginx site is independent of existing sites. Create the certificate using the existing ACME account and a
webroot challenge, then validate `nginx -t` before reloading. Renewal must retain the port-80 challenge location.
Keep a pre-change Nginx configuration backup and a persistent-data backup. Rollback changes the `current`
symlink only to a compatible release as required by the boundary above, and restarts this service. Never
replace current business data with stale data during rollback.

Login diagnostics: 运行设置 → 检测登录状态 performs a bounded login-token request without sending messages. Verification errors show only fixed error codes and an HTTPS verification link restricted to official Goofish/Taobao/Alibaba domains, visible only to the signed-in workspace via a CSRF-protected action. Links are not logged or persisted. Complete verification personally, then check again; browser verification may not clear server-IP restrictions.
