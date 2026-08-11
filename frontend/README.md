# ThreadStack Web

React/Vite web UI for the ThreadStack API.

## Development

```bash
npm install
npm run dev
```

The UI runs at <http://127.0.0.1:5173> and expects the API at `http://127.0.0.1:8000` by default. Override the API URL with `VITE_API_URL`.

When the API is unavailable or empty, the interface renders that state directly and does not substitute seeded data. The sidebar's **API & MCP docs** entry links to live Swagger documentation and summarizes agent authentication and MCP tools.

## Production Build

```bash
npm run build
npm run preview
```
