# ADK Channel Gateway

Standalone FastAPI service that connects external messaging channels to the ADK Travel Agents backend.

The gateway owns channel-specific concerns such as Telegram webhook validation, Twilio WhatsApp signatures, IMAP polling, SMTP replies, message dedupe, and channel-to-agent session mapping. The agent project remains responsible for the actual ADK agent run through `POST /api/chat/turn`.

## Architecture

```text
Telegram webhook ┐
Twilio WhatsApp  ├─ Channel Gateway ── POST /api/chat/turn ── Agent API
IMAP mailbox     ┘
       │
       └─ Dedicated Postgres database for session, dedupe, and subscription state
```

## Endpoints

- `GET /health`
- `POST /webhooks/telegram`
- `POST /webhooks/whatsapp/twilio`
- `POST /webhooks/microsoft/email`

For Microsoft 365 mailboxes, use the Microsoft Graph webhook path. IMAP polling is still available as a fallback for non-Microsoft mailboxes.

## Local Setup

From the channel gateway repository root:

```bash
cp .env.example .env
```

Start the agent API separately on port `8000`:

```bash
uvicorn app.main:app --reload --port 8000
```

Start the channel gateway:

```bash
docker compose up
```

The gateway listens on:

```text
http://localhost:8010
```

For local Docker-to-host agent calls, keep this value in `.env`:

```env
AGENT_API_BASE_URL=http://host.docker.internal:8000/api
```

If the agent API is containerized on the same Docker network, change it to the backend service name:

```env
AGENT_API_BASE_URL=http://agent-api:8000/api
```

## Dokploy Setup

Deploy this repository as its own Dokploy Compose service.

1. Create a new Dokploy Compose service.
2. Connect this channel gateway repository.
3. Set the Compose Path to `./docker-compose.yml`.
4. Add the production environment variables in Dokploy's Environment tab.
5. Add a domain that points to the `channel-gateway` service on port `8010`.

At minimum, set:

```env
AGENT_API_BASE_URL=https://YOUR_BACKEND_DOMAIN/api
GATEWAY_DATABASE_URL=postgresql+asyncpg://gateway:YOUR_PASSWORD@channel-gateway-postgres:5432/channel_gateway
GATEWAY_POSTGRES_PASSWORD=YOUR_PASSWORD
PUBLIC_APP_URL=https://YOUR_FRONTEND_DOMAIN
PUBLIC_GATEWAY_URL=https://YOUR_GATEWAY_DOMAIN
```

If the backend and gateway are deployed on the same Dokploy Docker network, `AGENT_API_BASE_URL` can use the internal backend service name instead:

```env
AGENT_API_BASE_URL=http://YOUR_BACKEND_SERVICE_NAME:8000/api
```

## Common Configuration

All secrets stay in `.env`. Do not commit real credentials.

Important shared settings:

```env
DEFAULT_AGENT=root
CHANNEL_RESULT_LIMIT=5
PUBLIC_APP_URL=http://localhost:5173
PUBLIC_GATEWAY_URL=https://YOUR_PUBLIC_DOMAIN
GATEWAY_DATABASE_URL=postgresql+asyncpg://gateway:gateway_password@channel-gateway-postgres:5432/channel_gateway
```

- `DEFAULT_AGENT` is the agent key sent to `/api/chat/turn`.
- `CHANNEL_RESULT_LIMIT` controls how many result-style artifact rows are included in plain text replies.
- `PUBLIC_APP_URL` is used when the agent indicates the user needs to connect or log in through the web app.
- `PUBLIC_GATEWAY_URL` is the public HTTPS base URL Microsoft Graph, Telegram, and Twilio can call.
- `GATEWAY_DATABASE_URL` controls the dedicated gateway Postgres database.

## Telegram Setup

1. Create a Telegram bot with BotFather.

   In Telegram, open `@BotFather`, run:

   ```text
   /newbot
   ```

   Copy the bot token.

2. Add Telegram credentials to `channel_gateway/.env`:

   ```env
   TELEGRAM_BOT_TOKEN=123456:your-bot-token
   TELEGRAM_WEBHOOK_SECRET=choose-a-long-random-string
   ```

3. Expose the gateway to the public internet.

   Telegram must reach:

   ```text
   https://YOUR_PUBLIC_DOMAIN/webhooks/telegram
   ```

   For local testing, use a tunnel such as ngrok or Cloudflare Tunnel pointed at `localhost:8010`.

4. Register the webhook:

   ```bash
   curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
     -H "Content-Type: application/json" \
     -d '{
       "url": "https://YOUR_PUBLIC_DOMAIN/webhooks/telegram",
       "secret_token": "choose-a-long-random-string"
     }'
   ```

5. Send a message to your bot.

   The gateway will:

   - validate `X-Telegram-Bot-Api-Secret-Token`
   - map the chat to `telegram:{chat_id}`
   - call `POST /api/chat/turn`
   - reply with Telegram `sendMessage`

6. Start a fresh agent session from Telegram:

   ```text
   /new
   ```

## WhatsApp Setup With Twilio

1. Create or open a Twilio account.

2. Enable the WhatsApp Sandbox or configure a production WhatsApp sender.

3. Add Twilio credentials to `channel_gateway/.env`:

   ```env
   TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   TWILIO_AUTH_TOKEN=your-twilio-auth-token
   TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
   ```

   For the Twilio sandbox, `TWILIO_WHATSAPP_FROM` is usually:

   ```env
   TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
   ```

4. Expose the gateway to the public internet.

   Twilio must reach:

   ```text
   https://YOUR_PUBLIC_DOMAIN/webhooks/whatsapp/twilio
   ```

5. Configure the Twilio inbound message webhook.

   In Twilio Console, set the WhatsApp "When a message comes in" URL to:

   ```text
   https://YOUR_PUBLIC_DOMAIN/webhooks/whatsapp/twilio
   ```

   Use:

   ```text
   HTTP POST
   ```

6. Send a WhatsApp message to the Twilio sandbox or production sender.

   The gateway will:

   - validate `X-Twilio-Signature`
   - parse `From`, `Body`, and `MessageSid`
   - map the sender to `whatsapp:{phone_number}`
   - call `POST /api/chat/turn`
   - reply through Twilio Messages API

7. Start a fresh agent session from WhatsApp:

   ```text
   new chat
   ```

## Microsoft 365 Email Webhook Setup

Use this for `aiva@travog.com`. This is the recommended email integration because Microsoft Graph sends webhook notifications when messages arrive, so replies can start immediately instead of waiting for an IMAP polling interval.

1. Create the mailbox in Microsoft 365.

   ```text
   aiva@travog.com
   ```

2. Register an app in Microsoft Entra ID.

   In Azure Portal:

   ```text
   Microsoft Entra ID -> App registrations -> New registration
   ```

   Save:

   - Tenant ID
   - Client ID
   - Client secret

3. Add Microsoft Graph application permissions.

   Required permissions:

   ```text
   Mail.Read
   Mail.Send
   ```

   Grant admin consent after adding the permissions.

4. Add Microsoft Graph settings to `channel_gateway/.env`:

   ```env
   PUBLIC_GATEWAY_URL=https://YOUR_PUBLIC_DOMAIN
   MICROSOFT_TENANT_ID=your-tenant-id
   MICROSOFT_CLIENT_ID=your-client-id
   MICROSOFT_CLIENT_SECRET=your-client-secret
   MICROSOFT_MAILBOX=aiva@travog.com
   MICROSOFT_WEBHOOK_CLIENT_STATE=choose-a-long-random-string
   MICROSOFT_SUBSCRIPTION_RENEWAL_SECONDS=3600
   ```

5. Expose the gateway through public HTTPS.

   Microsoft Graph must reach:

   ```text
   https://YOUR_PUBLIC_DOMAIN/webhooks/microsoft/email
   ```

   Microsoft validates this endpoint by sending a `validationToken` query parameter. The gateway responds with that token as plain text.

6. Start the gateway.

   On startup, if Microsoft Graph settings are present, the gateway creates or renews a subscription for:

   ```text
   /users/aiva@travog.com/mailFolders('Inbox')/messages
   ```

7. Send an email to `aiva@travog.com`.

   The gateway will:

   - receive a Microsoft Graph change notification
   - validate `clientState`
   - fetch the full message through Graph
   - dedupe by `internetMessageId` or Graph message ID
   - map the sender to `email:{sender_address}`
   - call `POST /api/chat/turn`
   - reply through Microsoft Graph `sendMail`

8. Subscription renewal is automatic.

   Microsoft Graph subscriptions for Outlook messages expire in under 7 days. The gateway stores the subscription ID in Postgres and renews it periodically.

## Email Polling Setup

Use this only as a fallback for non-Microsoft mailboxes. Microsoft 365 should use the Graph webhook setup above.

Email polling uses IMAP for inbound messages and SMTP for outbound replies.

1. Create or choose a dedicated mailbox.

   Example:

   ```text
   travel-agent@example.com
   ```

2. Enable IMAP and SMTP for that mailbox.

   For Gmail or Google Workspace, use an app password or OAuth-backed SMTP/IMAP credentials, depending on your organization settings.

3. Add IMAP settings to `channel_gateway/.env`:

   ```env
   EMAIL_IMAP_HOST=imap.example.com
   EMAIL_IMAP_PORT=993
   EMAIL_IMAP_USER=travel-agent@example.com
   EMAIL_IMAP_PASSWORD=your-imap-password
   EMAIL_IMAP_FOLDER=INBOX
   EMAIL_POLL_INTERVAL_SECONDS=30
   ```

4. Add SMTP settings to `channel_gateway/.env`:

   ```env
   EMAIL_SMTP_HOST=smtp.example.com
   EMAIL_SMTP_PORT=587
   EMAIL_SMTP_USER=travel-agent@example.com
   EMAIL_SMTP_PASSWORD=your-smtp-password
   EMAIL_FROM=travel-agent@example.com
   ```

5. Start the gateway.

   The IMAP poller starts automatically in the FastAPI lifespan. If `EMAIL_IMAP_HOST` or `EMAIL_IMAP_USER` is blank, polling is skipped.

6. Send an email to the mailbox.

   The gateway will:

   - search for unseen messages
   - dedupe by `Message-ID` or IMAP UID
   - extract the plain text body
   - map the sender to `email:{sender_address}`
   - thread using `References` / `In-Reply-To` when available
   - call `POST /api/chat/turn`
   - reply through SMTP with `Re: <original subject>`

## Plain Text Responses

Telegram, WhatsApp, and email receive plain text only. The agent can still produce UI artifacts for the web app, but the gateway summarizes artifact metadata and limits list-style output to `CHANNEL_RESULT_LIMIT`.

Default:

```env
CHANNEL_RESULT_LIMIT=5
```

If a response is too long for WhatsApp or Telegram, reduce this to `3`.

## Quick Local Checks

Check the gateway is running:

```bash
curl http://localhost:8010/health
```

Expected:

```json
{"status":"ok"}
```

Send a local Telegram-shaped request:

```bash
curl -X POST http://localhost:8010/webhooks/telegram \
  -H "Content-Type: application/json" \
  -H "X-Telegram-Bot-Api-Secret-Token: $TELEGRAM_WEBHOOK_SECRET" \
  -d '{
    "message": {
      "message_id": 1,
      "text": "Find flights from Delhi to Mumbai tomorrow",
      "chat": { "id": 12345 }
    }
  }'
```

Send a local Twilio-shaped request:

```bash
curl -X POST http://localhost:8010/webhooks/whatsapp/twilio \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "From=whatsapp:+15551234567" \
  --data-urlencode "To=whatsapp:+14155238886" \
  --data-urlencode "Body=Hello" \
  --data-urlencode "MessageSid=SM_LOCAL_TEST"
```

If `TWILIO_AUTH_TOKEN` is set, local Twilio-shaped requests must include a valid `X-Twilio-Signature`. For quick unsigned local tests, leave `TWILIO_AUTH_TOKEN` blank.

## Troubleshooting

- Gateway cannot reach the agent API:
  - Confirm the agent API is running on `http://localhost:8000`.
  - In Docker, use `AGENT_API_BASE_URL=http://host.docker.internal:8000/api`.
- Telegram does not call the webhook:
  - Confirm the public URL is HTTPS.
  - Confirm the registered webhook path is `/webhooks/telegram`.
  - Confirm `TELEGRAM_WEBHOOK_SECRET` matches the `secret_token` used with `setWebhook`.
- Twilio returns webhook errors:
  - Confirm the configured URL is public HTTPS.
  - Confirm the Twilio auth token matches `TWILIO_AUTH_TOKEN`.
  - Confirm the webhook method is `POST`.
- Email polling does nothing:
  - Confirm `EMAIL_IMAP_HOST` and `EMAIL_IMAP_USER` are set.
  - Confirm the mailbox has unseen messages.
  - Confirm IMAP is enabled for the mailbox.
- Email replies do not send:
  - Confirm SMTP host, port, user, password, and `EMAIL_FROM`.
   - Confirm the SMTP provider permits app-password or basic SMTP login.
