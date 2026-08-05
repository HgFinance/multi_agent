# Backend API

This is the extensible FastAPI boundary for the platform. It intentionally does not replace the existing read-only `apps/api` BFF yet.

```bash
uvicorn backend.app.main:app --reload --port 8002
```

OpenAPI is available at `http://localhost:8002/docs`.

- `GET /api/v1/portfolio?include_stock=true&include_derivatives=true`
- `GET /api/v1/agent/health`
- `POST /api/v1/agent/invoke`

The public portfolio contract contains stocks and derivatives only. The LangGraph adapter is a transport boundary and must be replaced with the department-owned endpoint contract as teams align on it.
