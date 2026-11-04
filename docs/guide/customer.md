# Customer guide

This guide is for **customers** of a Nexaploy platform: you run Frappe / ERPNext sites on the
platform's shared hosting, on a server the platform creates for you, or on **your own server**
(BYOS). Everything happens in the dashboard at `https://<platform>/dashboard`. Platform owners
read the [operator guide](operator.md).

The dashboard is available in English and Arabic. Change the language in **Account → Language**
(Arabic is shown right-to-left).

## 1. Sign up and log in

There are two ways to get an account:

- **Self sign-up for a product** (e.g. ERPNext): open the sign-up link the platform gives you
  (`/signup/<product>`), enter *Full name*, *E-mail*, *Company* (optional), *Site address* and
  *Password*, then click **Create my site**. Confirm the link sent to your inbox. Your site is
  created in a few minutes; log in to it as `Administrator` with the password you chose. Trials
  are free for the number of days shown, without a card.
- **Invitation** from an existing team (see below), or an account created by the operator.

On first use you get a personal team. Log in at `https://<platform>/login`.

## 2. Teams, roles and security

Everything (servers, sites, backups, invoices) belongs to a **team**. Switch teams in the
sidebar; create another one in **Team → New team**.

| Role | Can do |
|---|---|
| **Owner** | everything, including other Owners |
| **Admin** | members, invitations, servers, API keys, billing, sites |
| **Developer** | sites, bench groups, apps, backups, domains, config |
| **Billing** | invoices, payments, plans, credits |

Everybody can view. Only an Owner can invite or remove another Owner, and a team always keeps
one Owner.

### Invite a member

1. **Team → Invitations**: enter the *Email address*, choose the role and click **Invite**.
2. The invitee opens the e-mailed link (valid 7 days) and clicks **Accept invitation** while
   logged in with that e-mail address. If the e-mail could not be sent, copy the link shown.
3. Change a role or **Remove** a member in **Members**; **Revoke** a pending invitation.

### Two-factor authentication (2FA)

- Owners/Admins: **Team → Security → Require two-factor authentication for all members**.
  Members are asked for a one-time code from an authenticator app at their **next** sign-in.
- **Account → Two-factor authentication** shows your own status.
- API keys do not use 2FA; keep them secret.

### Account and API keys

**Account** has your *Profile*, *Language*, **Change password** and **API access**:
**Generate API key** shows the secret **once**; **Rotate** issues a new pair; **Revoke** deletes it.

```bash
curl -s https://<platform>/api/method/nexaploy.api.dashboard.list_sites \
  -H "Authorization: token <api_key>:<api_secret>" \
  -H "X-Nexaploy-Team: <team>"
```

### Activity log

**Activity** (Owners/Admins) lists every change in the team: who, what, when, from which IP
address. Filter by action, member and dates. It cannot be edited or deleted.

## 3. Where your sites run

| Option | You pay | Who manages the server |
|---|---|---|
| **Shared hosting** | a site plan per site | the platform |
| **Hosted server** (**Servers → Create hosted server**) | a server plan | the platform creates it on Hetzner/DigitalOcean; it is yours alone |
| **Your own server (BYOS)** | a management fee per server; the VM to your provider | you own it; Nexaploy manages benches and sites |

### Create a hosted server

1. **Servers → Create hosted server**.
2. Choose *Provider*, *Region*, *Server size*, optionally a *Server name*. The price per month is
   shown.
3. **Create server**. Setup takes 5–10 minutes; then **Open server**.

### Add your own server (BYOS)

You need a fresh **Ubuntu 22.04 or 24.04** server (x86_64 or arm64) with at least 2 GB RAM
(4 GB or more recommended), 20 GB free disk and ports **80 and 443** open to the internet.

1. **Servers → Add your own server → Generate install command**.
2. Log in to your server and run the command shown, as root:

    ```bash
    curl -fsSL https://<platform>/install.sh | sudo bash -s -- \
      --token <registration-token> --control-plane https://<platform>
    ```

3. Watch the page: *Waiting for your server to register…* → *Server registered, waiting for the
   first heartbeat…* → *Your server is connected and active.*
4. The token can be used once and expires (24 hours). If it expired, click **Generate a new
   command**.

!!! note "The installer updates your operating system first"
    Before installing anything, `install.sh` runs a full system upgrade (`apt-get update`,
    `apt-get full-upgrade` keeping your configuration files, then `autoremove`). This also
    installs the newest kernel. If a reboot is needed, the installer finishes the installation
    and then **reboots the server automatically 1 minute later** (in interactive mode it asks
    first). Your sites come back by themselves after the reboot.

    | Option | Effect |
    |---|---|
    | `--no-os-upgrade` | skip the system upgrade |
    | `--hwe-kernel` | Ubuntu: also install the newer HWE kernel |
    | `--no-reboot` | never reboot automatically; reboot later yourself |
    | `--acme-email you@example.com` | e-mail for Let's Encrypt certificates (recommended) |

The platform never needs SSH or open ports on your server: the agent connects out. If you
remove the agent (`systemctl disable --now nexaploy-agent-api nexaploy-agent-worker`), your
sites keep running.

### Your server page

Open a server from **Servers** to see its status, *Last heartbeat*, *Agent version*, *Facts*,
benches and recent jobs. A server without heartbeat for 5 minutes shows *Offline*: check that it
runs and that the `nexaploy-agent` services are active. Your servers show a **health badge**
(OK, Warning, Critical, Unknown, Offline) with the issues found and a hint for each.

If the platform allows it, Owners and Admins can also **Upgrade agent** / **Roll back agent**
on their own servers.

### System updates on your own server

Owners and Admins see a **System updates** card on their own BYOS servers:

- pending updates and how many are security updates;
- the running kernel and the newest installed kernel, and **Reboot required** when a reboot is
  needed;
- **Update now**: all updates or security only, and reboot *never* or *if required*;
- **Reboot now**;
- **Maintenance window**: a weekly day and hour (UTC) for updates, with automatic reboot if
  required;
- **Automatic security updates**: installs security updates every day, never reboots by itself.

Reboots happen only after the job reports success. Your sites are briefly unavailable during a
reboot and come back by themselves.

## 4. Bench groups and Frappe versions

A **bench group** is a Frappe version plus a list of apps. Each deploy builds one Docker image,
which runs as a *bench* on a server. Several Frappe versions (**14, 15, 16**) run side by side
on one server; new groups default to v16.

1. **Bench Groups → New Bench Group**.
2. Enter a *Name* (lowercase, digits, dashes), pick the *Frappe version* and the *Build server*.
3. Tick **Marketplace apps** and **Your custom apps**. Apps that do not support the chosen
   version are marked *Not compatible with this Frappe version*.
4. **Create bench group**, then on the group page click **Deploy now**. The first build takes a
   while; the build log is shown live under *Deploys*.
5. Under **Benches on your servers** choose a server and click **Deploy to server**.

To add an app later: **Add app** on the group page, then **Deploy** again. **Auto update** with
an update window deploys new builds automatically. *Update available* appears when new commits
were pushed to your custom apps.

### Your own apps from GitHub

1. **Bench Groups → Custom Apps → Connect GitHub** and install the platform's GitHub App on your
   account or organisation.
2. **Add an app from GitHub**: choose the account, repository and branch; the app name and the
   supported Frappe versions are detected.
3. Other providers: **Add from another git provider (GitLab, Gitea, …)** with the repository URL
   and a read-only access token.

## 5. Sites

### Create a site

1. **Sites → New Site**.
2. **Choose where to deploy**: *Your servers* (a bench of yours) or *Shared hosting*.
3. Enter the *Site name*. On shared hosting it becomes `<name>.<platform domain>`; on your own
   server you can enter a full domain such as `erp.example.com`.
4. Tick the *Apps* and choose a *Plan* (on your own server: *No plan (included in your server)*).
5. **Create site**. **Copy the Administrator password now**: it is not shown again.

The site page has the buttons **Open site**, **Open desk**, **Insights**, **Safe update** and
**Get help**, and the tabs **Apps, Domains, Backups, Usage, Config, E-mail, Lifecycle, Storage,
Jobs, Settings**.

### Install apps

**Apps → Available apps → Install**. Only apps that are in the bench's image and support the
site's Frappe version are listed. If an app needs other apps, they are installed first (you are
told which). To get more apps, add them to the bench group and deploy. **Uninstall** removes the
app and its data from the site.

The **Marketplace** page lists apps with filters for *Frappe version* and *Category*, the
supported versions (*Supports Frappe …*), requirements (*Requires: …*), price, ratings and
reviews. Teams that have an app installed can **Rate this app** and **Save review**.

### Custom domains

1. Create the DNS record at your DNS provider **first**: a **CNAME** to the site's address for a
   subdomain (`erp.example.com`), or an **A/AAAA** record to the server's IP for a root domain
   (`example.com`). The **Domains** tab lists the exact records (*Recommended* first, with
   **Copy**).
2. **Domains → Add domain**.
3. Nexaploy checks DNS. Status: *Pending DNS* → *Verifying* → *Active* (or *Broken*). It keeps
   checking automatically for 72 hours; click **Verify now** to check at once.
4. When the domain is active a free Let's Encrypt certificate is issued. **Check certificates**
   shows the TLS status (*Valid*, *Expiring*, *Default Certificate*, …).

!!! warning "Cloudflare"
    With the orange-cloud proxy on, the record does not show the server's IP. The page explains
    this; turn the proxy off (DNS only) or ask the operator to skip the DNS check.

### Site config

**Config → Reload** reads `site_config.json` from the server. Edit values (JSON: numbers,
`true/false`, lists and objects are typed), **Add key**, then **Save changes**. Secrets are
hidden; database, encryption and Nexaploy-managed keys are read-only.

### Usage and plan

**Usage** shows today's CPU time, requests, database and file size against the plan, and 30 days
of history. *Over plan limit* may lead to throttling or maintenance mode, depending on the plan.
Owners, Admins and Billing members can **Upgrade plan** / **Switch**.

### Settings

**Migrate** runs `bench migrate`. **Drop site** takes a backup and deletes the site (type the name
to confirm).

## 6. Backups and restore

### Backups of one site

**Site → Backups**: tick **Include files** and click **Backup now**. The list shows each backup
with type, files, size and status; **Download** gives a 15-minute link (S3 storage only);
**Restore** puts that backup back into the site.

Scheduled backups run automatically (daily by default; the frequency is set by the platform).

### Backup destinations

By default backups go to the platform's storage. Owners/Admins can choose their own on the
**Backups** page:

1. **Add destination** → *Type*: **Google Drive**, **OneDrive**, **Dropbox** or **S3**.
2. Enter a *Name* (e.g. "Company Drive") and a *Folder*. Each server writes into a sub-folder
   named after its server ID.
3. Google Drive / OneDrive / Dropbox: **Save and connect**, sign in at the provider and allow
   access. (Drive: Nexaploy only sees the files it creates.)
   S3: enter *Bucket*, *S3 provider*, *Endpoint*, *Region*, *Access key ID*, *Secret access key*,
   then **Save**.
4. **Test** checks the connection (and shows the quota).
5. Tick **Use for all team servers without their own destination**, or choose a destination per
   server in the *Servers* table (*Backups go to*).

**Reconnect** renews an expired authorisation; **Disconnect** removes the credentials.

### Retention

On the **Backups** page, **Retention** sets the team default:

| Setting | Meaning |
|---|---|
| Keep backups in storage (days) | remote copies; 0 = keep forever |
| Keep on the server (days) | local copies; 0 = delete right after upload |
| Daily / Weekly / Monthly backups to keep | extra copies kept beyond the days |

**Save team default**. Per site you can switch from *Team default* to *Custom*. The platform sets
minimum and maximum values; longer retention may be a paid add-on.

### Restore (all, database or files, or into a new site)

1. **Backups → Restore…** next to the site.
2. Choose a backup and **Restore what**: *Database and files*, *Database only* or *Files only*.
3. **Restore into this site** (replaces the current data) or **Restore as new site** with a
   *New site name* and *Bench* to inspect the backup safely.

### Point-in-time restore

"Bring my site back to how it was at 15:15 today."

1. **Site → Backups → Restore to a point in time**. The window is shown (*Available from … to …*,
   last 7 days, up to a few minutes ago).
2. Pick the time (your browser's time zone).
3. Choose **Restore into a new site to inspect it first (recommended)**: e-mails are muted and the
   scheduler is paused on the copy; it is billed like any site until you drop it. Or **Overwrite
   this site**: everything after that time is lost; a safety backup is taken first; type the site
   name to confirm.

## 7. Staging, safe update and version upgrades

### Staging site

**Lifecycle → Create staging site** makes a copy (`staging-<site>`) on the same bench: e-mails
muted, scheduler paused, e-mail accounts disabled, not billed. **Refresh from production**
replaces its data. Drop it with the normal **Drop site**.

### Safe update

1. **Safe update** on the site page.
2. Choose the *Target bench* (default: *Newest build of this bench group*) and click **Test
   update**. A copy is created on a bench running the new version, migrated and checked (ping,
   login page, database). Your site is not changed.
3. If the test *Passed*, click **Apply update** (a fresh backup is taken first). Tick *Delete the
   test site afterwards*, or **Delete test site** later.
4. If it *Failed*, read the log (**Show log**); your site stays as it is.

Bench group owners can enable **Only update automatically after every site passed an update
test**.

### Move or upgrade v15 → v16

1. Create a v16 bench group with the same apps (each must support v16), deploy it and deploy it
   to a server.
2. **Lifecycle → Move site**: choose the target bench. For a higher version tick
   **Upgrade from v15 to v16 (runs migrate; cannot be undone)**. Click **Move**.
3. The site is backed up, restored on the target bench, migrated and removed from the old bench.
   Custom domains are kept. Moving to another server needs platform backup storage, and every
   domain must then point to the new server. A lower version is refused.

!!! tip
    Test first: run a **Safe update** to the v16 bench, or restore a backup as a new site there.

### Rename

**Lifecycle → Rename site** → *New name* → **Rename**. Database and files stay; the old name is
kept as a domain, so old links still work. The site is briefly in maintenance mode.

### Transfer to another team

1. **Lifecycle → Transfer to another team**: enter the team or an e-mail, **Request transfer**.
2. Owners/Admins of the receiving team open the link and click **Accept transfer** (or
   **Decline**). From then on they own and pay for the site; backups move with it.

Only sites on shared hosting can change teams; move a site off your own server first.

## 8. Insights, console and audit

**Insights** on the site page has these tabs:

| Tab | What | Who (default) |
|---|---|---|
| Analytics | requests, median and 95th percentile response, background jobs, top paths, status codes | everyone |
| Logs | Web, Background workers, Scheduler, Frappe log, Error Log | Owner, Admin, Developer |
| Slow queries | grouped slow database queries | Owner, Admin, Developer |
| SQL | **Run query**, **Read-only** by default, **Download CSV** | read-only: Owner, Admin, Developer; write: Owner, Admin |
| Console | Python **Execute** inside the site as Administrator; **Commit database changes** optional | Owner, Admin |

!!! warning
    Write-mode SQL and committed console code change data immediately and cannot be undone. Take
    a backup first. The console is refused on benches shared with other teams.

**Activity → Console access** (Owners/Admins): change each member's permissions (*Custom* or
*Role defaults*), **Require two-factor authentication for SQL and the console**, and read the
**Console audit log**: every query, console run and log view, including refused attempts. It
cannot be edited or deleted.

## 9. E-mail and storage add-ons

### Managed e-mail

1. **Site → E-mail → Enable managed e-mail**. The site gets an outgoing Email Account
   "Nexaploy Relay"; mail is sent through the platform relay (with a daily limit).
2. Optional **Custom sending domain**: enter your domain and **Save**, create the SPF, DKIM and
   DMARC records shown, then **Check DNS**. Once *Verified*, mail is sent as `noreply@<your
   domain>`. Until then the default sender is used.

### Offsite attachments, DR replica and trash bin

Paid add-ons (if the platform offers them), in **Site → Storage**:

1. **Attachments storage**: *On the server's disk* or *On object storage*.
2. Choose the *Primary bucket*, optionally a **Replica (disaster recovery)** in another region
   and a **Trash bin for deleted files** with *Keep deleted files for (days)*.
3. Save. Files are moved online; the site keeps running.
4. **Check status** shows where files are served from, the last replication and the mount health.
5. If the primary region is down: **Fail over to replica**; later **Switch back to primary**.
6. **Show deleted files** lists the trash bin; restore a file from there.

Backups of an offloaded site contain only the database (the files are already off the server).

## 10. Uptime monitoring and status page

Every active site gets a default monitor automatically. In **Monitoring**:

- **Add monitor**: *Name*, *Site*, *Address* (the site or an active custom domain), *Path*,
  *Method*, *Expected status* (`200`, `200-299`, `2xx`), *Keyword (optional)*, *Interval*,
  *Timeout (seconds)*, *Failures before alerting*, *Quorum*, *Probes*. **Save monitor**.
  1-minute checks and more than 3 monitors per site need the advanced monitoring feature.
- **Pause** / **Resume** / **Edit** / **Delete** a monitor (the default one can only be paused).
- **Incidents**: set a *Public title*, **Hide from the status page**, **Post an update**.
- **Status page**: *Title*, *Description*, *Custom domain (optional)*, *Published*, *Show incident
  history*, *Monitors on the page* with a *Public name*. **Save status page**, then **Open status
  page** (`/status/<slug>`). Visitors see only public names, never URLs or errors. For a custom
  domain create a CNAME to the control plane and ask the operator to route it.

## 11. Notifications

By default alerts and billing notices are e-mailed to your team. **Notifications → Add channel**
(Owners/Admins) sends them elsewhere. Once you add a channel, notifications go to your channels
only (an *Email* channel is added automatically so e-mail keeps working; disable it if you do not
need it).

Events: Server offline, Server back online, Disk almost full, Unhealthy containers, Site down,
Site back up, Backup failed, Job failed, Invoice issued, Payment failed, Sites suspended, Support
reply, Monitor down, Monitor back up, and server health critical / back to OK.

### Telegram, step by step

1. In Telegram open **@BotFather**, send `/newbot`, choose a name and a username ending in `bot`.
   BotFather replies with the **bot token** (`123456:ABC…`).
2. Open your new bot and send it any message (e.g. `/start`). For a group: add the bot to the
   group and send `/start@<your bot username>` in the group.
3. In the dashboard: **Notifications → Add channel**, *Type* **Telegram**, a *Name*, paste the
   **Bot token**.
4. Click **Detect chat** and pick your chat (or group). The *Chat ID* is filled in. If nothing
   is found: "No chats yet. Send a message to your bot, then try again."
5. Tick the **Events**, keep *Enabled*, **Save channel**.
6. **Send test**. The status becomes *Working*.

### WhatsApp (Meta Cloud API)

*Phone number ID* and *Access token* from your Meta WhatsApp Business app, *Recipient number*
(international format), optionally *Template name* and *Template language* (e.g. `en_US`, `ar`).
Without an approved template WhatsApp only delivers messages within 24 hours after the recipient
last wrote to your number; with a template (one body variable `{{1}}`) it always does.

### Slack and Discord

Create an incoming webhook (Slack: `https://hooks.slack.com/services/…`; Discord: channel
settings → Integrations → Webhooks, `https://discord.com/api/webhooks/…`) and paste it as
*Webhook URL*.

### Generic webhook and signature verification

*Webhook URL* (https, public address) and an optional *Signing secret*. Each event is POSTed as
JSON `{event, title, message, context, url, team, timestamp}`. With a secret the header
`X-Nexaploy-Signature: sha256=<hex>` holds the HMAC-SHA256 of the **raw body**. Verify it before
trusting the request:

=== "Python"

    ```python
    import hashlib, hmac

    def verify(raw_body: bytes, header: str, secret: str) -> bool:
        expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, header or "")
    ```

=== "Node.js"

    ```javascript
    const crypto = require("crypto");

    function verify(rawBody, header, secret) {
      const expected = "sha256=" + crypto.createHmac("sha256", secret).update(rawBody).digest("hex");
      const a = Buffer.from(expected), b = Buffer.from(header || "");
      return a.length === b.length && crypto.timingSafeEqual(a, b);
    }
    ```

Use the raw request bytes, not re-serialised JSON.

## 12. Billing

**Billing** (Owners, Admins and Billing members):

- **Credit balance**: prepaid credits pay new invoices automatically before any card is charged.
  **Add credits** → *Amount*, *Pay with* (Stripe or Paymob) → **Top up**.
- **Credit history**: every top-up, payment and adjustment.
- **Invoices**: one per month for the previous month (sites, servers, paid apps, add-ons) with
  *Credits applied* and *Amount due*. **Pay** opens the payment page.

!!! warning "Suspension"
    An unpaid invoice becomes *Overdue* after its due date. After the number of days set by the
    platform, the team's sites are **suspended** (maintenance mode). Paying the invoice lifts the
    suspension automatically.

## 13. Support

1. **Support → New ticket** (or **Get help** on a site page).
2. *Subject*, *Priority* (the expected first response time is shown), *Site* or *Server*,
   *Describe the problem*. **Open ticket**.
3. Replies arrive by e-mail; answer with **Send reply**. **Close ticket** when done; **Reopen** if
   needed. Resolved tickets close after 7 days.

Never share passwords or API secrets in a ticket.

## 14. Publishing your own apps

1. **Publisher → Become a publisher**: *Publisher name*, *Website*, *Support email*, *Payout
   method* (Bank, PayPal or Credits) and payout details.
2. **New listing**: choose one of your custom apps. The repository must be **public**.
3. Fill *Title*, *Category*, *Pricing* (Free / Paid with *Price per site per month*),
   *Description*, *Logo URL*, *Documentation URL*, *Required apps*, *Screenshots*.
4. **Submit for review**. Staff approve or reject it with notes; **Withdraw** to edit while in
   review. On approval one release per Frappe version is created; each release is test-built
   before customers can install it.
5. **Revenue by month** and **Payouts** show your share (default 80 %), paid monthly.

## 15. FAQ

**What happens to my sites if I leave?** They keep running. Removing the agent leaves benches,
sites, database and proxy running as plain Docker Compose projects in `/opt/nexaploy`.

**Which versions are supported?** Frappe/ERPNext 14, 15 and 16, side by side.

**Does the platform need SSH to my server?** No. Only ports 80/443 for your sites.

**Why is my domain still "Pending DNS"?** The record does not point to the server yet, or a
Cloudflare proxy hides it. DNS changes can take time; click **Verify now**.

**My site shows a certificate warning.** The domain must point to the server and port 80 must
be open. Check **Domains → Check certificates**.

**Can I restore just one deleted record?** Restore a backup or a point in time **into a new
site**, then copy the data over.

**Can I download my backups?** Yes, from **Site → Backups → Download** when the storage is S3;
Drive/OneDrive/Dropbox backups are in your own cloud drive.

**Why is my site in maintenance mode?** It is over its plan limits (see **Usage**), an invoice is
overdue, or a restore/rename is running.

**Will a system update break my sites?** No. Updates run on the host; the sites come back by
themselves after a reboot.
