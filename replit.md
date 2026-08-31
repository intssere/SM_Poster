# Replit setup

This project keeps its existing React/Vite frontend and FastAPI backend.

## Running on Replit

- `Start application` runs `cd frontend && npm run dev` on port 5000.
- `Backend API` runs `cd backend && alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- Vite proxies `/api` requests to the backend, so the dashboard can use the backend through the same preview.
- Replit PostgreSQL supplies `DATABASE_URL` and the `PG*` variables. Do not add a local Docker database for the Replit workflows.
- The backend workflow applies the development schema before starting the API.

## Environment

- `PUBLISHING_ENABLED=false` must remain set while Pinterest publishing is not approved.
- `SESSION_SECRET` is accepted as the application secret fallback. Optional Shopify, Pinterest, object storage, and AI credentials remain unset until configured.
- Shopify's primary authentication uses `SHOPIFY_SHOP`, `SHOPIFY_CLIENT_ID`, and `SHOPIFY_CLIENT_SECRET`. The server exchanges these for a short-lived Admin API token and keeps it server-side.
- `SHOPIFY_ACCESS_TOKEN` remains an optional legacy fallback. `SHOPIFY_API_VERSION` defaults to `2026-07`.
- The Shopify app version must grant `read_products` and `read_inventory`.
- Do not expose Shopify credentials to the frontend or add redirect-based Shopify/Pinterest OAuth for this catalog flow.