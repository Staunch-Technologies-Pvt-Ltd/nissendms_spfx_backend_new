# AI BANTO Email Automation API (FastAPI + Microsoft Graph)

A small FastAPI service that an SPFx web part calls directly (REST) to send
a correctly-tagged email into AI BANTO via Microsoft Graph `sendMail`.

## How it works

1. SPFx web part collects: DataSource tag (dropdown), vessel name, optional
   subject text, optional body, optional attachments (as base64).
2. It `POST`s that to `/send-email` on this API.
3. The API validates the tag against the DataSource table from the AI BANTO
   manual. If it's missing/invalid/misspelled, it falls back to `mail`
   (matching AI BANTO's own documented default behavior).
4. It builds the subject as `[DataSource:TAG] Vessel Name / Subject`.
5. It sends the email via Microsoft Graph, **as an app**, to the fixed
   recipient `erpsupport@staunchtec.com`.

## 1. Azure AD App Registration (one-time setup)

1. Go to **Azure Portal → Azure Active Directory → App registrations → New registration**.
   - Name: `ai-banto-email-service` (or similar)
   - Supported account types: single tenant
   - No redirect URI needed (this is a daemon/app-only flow)
2. Note the **Application (client) ID** and **Directory (tenant) ID**.
3. Go to **Certificates & secrets → New client secret**. Copy the secret value
   immediately (shown once).
4. Go to **API permissions → Add a permission → Microsoft Graph → Application permissions**.
   - Add `Mail.Send`
   - Click **Grant admin consent** (requires a Global/Application admin).

### Restrict which mailbox the app can send from (important)

`Mail.Send` as an *application* permission by default lets the app send as
**any** mailbox in the tenant. Scope it down using an Application Access
Policy (run in Exchange Online PowerShell by a tenant admin):

```powershell
Connect-ExchangeOnline

New-ApplicationAccessPolicy `
  -AppId "<your-client-id>" `
  -PolicyScopeGroupId "notifications@yourtenant.com" `
  -AccessRight RestrictAccess `
  -Description "Restrict ai-banto-email-service to one mailbox"
```

This ensures the app registration can only send mail as the one mailbox
you designate (e.g. a shared mailbox like `notifications@yourtenant.com`),
not impersonate arbitrary users.

## 2. Set up PostgreSQL

Every request to `/send-email` is now persisted: the full request details
(tag, vessel name, subject, body, recipient, resolved tag, send status) plus
each attachment's raw file bytes are stored in Postgres.

**Quickest way to get a local Postgres running (Docker):**

```bash
docker run --name ai-banto-postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=ai_banto_email \
  -p 5432:5432 -d postgres:16
```

Or install Postgres natively and create the database:

```bash
createdb ai_banto_email
```

Tables (`email_log`, `email_attachment`) are created automatically on
startup via `Base.metadata.create_all()` — no manual migration needed for
a first run. For ongoing schema changes in production, switch to
[Alembic](https://alembic.sqlalchemy.org/) migrations instead of relying on
`create_all`.

**Note on storing files in the database:** attachments are stored as raw
bytes (`bytea`) directly in Postgres for simplicity. This is fine for
typical document/certificate-sized attachments, but if you expect very
large or very high-volume files, consider storing them in blob storage
(e.g. Azure Blob Storage) and keeping only a reference/path in the
`email_attachment` table instead — ask if you'd like that swapped in.

## 3. Configure environment variables

```bash
cp .env.example .env
# fill in AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET,
# GRAPH_SENDER_MAILBOX, and ALLOWED_ORIGINS (your SharePoint tenant URL)
```

## 4. Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --reload-dir app --port 8000
```

Test it:

```bash
curl -X POST http://localhost:8000/send-email \
  -H "Content-Type: application/json" \
  -d '{
        "datasource_tag": "contract",
        "vessel_name": "DUCHESS EMERALD",
        "body": "<p>Please see attached CP.</p>"
      }'
```

Browse the DataSource tag list (useful for populating the SPFx dropdown):

```bash
curl http://localhost:8000/datasource-tags
```

## 5. Calling it from the SPFx web part

Since SPFx calls this directly by REST, use the SPFx HttpClient (not
SPHttpClient, since this is an external API, not SharePoint's own REST API):

```typescript
import { HttpClient, HttpClientResponse } from '@microsoft/sp-http';

interface ISendEmailPayload {
  datasource_tag: string;
  vessel_name?: string;
  subject_text?: string;
  body?: string;
  attachments?: { filename: string; content_base64: string; content_type?: string }[];
}

private async sendToAiBanto(payload: ISendEmailPayload): Promise<void> {
  const response: HttpClientResponse = await this.context.httpClient.post(
    'https://your-fastapi-host/send-email',
    HttpClient.configurations.v1,
    {
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }
  );

  if (!response.ok) {
    const errText = await response.text();
    throw new Error(`Email send failed: ${response.status} ${errText}`);
  }

  const result = await response.json();
  console.log('Sent:', result.subject);
}
```

Example call with an attachment converted to base64 in the browser:

```typescript
const file: File = this.selectedFile;
const arrayBuffer = await file.arrayBuffer();
const base64 = btoa(
  new Uint8Array(arrayBuffer).reduce((data, byte) => data + String.fromCharCode(byte), '')
);

await this.sendToAiBanto({
  datasource_tag: 'right_ship',
  vessel_name: 'DUCHESS EMERALD',
  body: '<p>RightShip inspection report attached.</p>',
  attachments: [{ filename: file.name, content_base64: base64, content_type: file.type }]
});
```

## 6. Deployment notes

- CORS is already configured in `app/config.py` (`ALLOWED_ORIGINS`) — set it
  to your SharePoint tenant's origin(s), e.g. `https://yourtenant.sharepoint.com`.
- Host this anywhere that can run a container/Python process (Azure App
  Service is a natural fit alongside SharePoint Online / Azure AD).
- Keep `AZURE_CLIENT_SECRET` out of source control — use App Service
  Application Settings / a secrets manager in production, not `.env`.
- Consider adding auth on the FastAPI side too (e.g. requiring an Azure AD
  token from the SPFx user, validated with a library like `python-jose`)
  so the endpoint isn't wide open to anyone who discovers the URL.

## Project structure

```
app/
  config.py        # DataSource tag table, recipient, Graph/CORS/DB settings
  models.py         # Pydantic request/response models
  database.py       # SQLAlchemy engine/session setup (PostgreSQL)
  db_models.py       # EmailLog + EmailAttachment ORM models
  graph_client.py   # MSAL token + Graph sendMail call
  main.py           # FastAPI app, tag validation, subject builder, endpoints
requirements.txt
.env.example
```
