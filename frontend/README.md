# AegisExchange Web

React/Vite web UI for the AegisExchange API.

## Development

```bash
npm install
npm run dev
```

The UI runs at <http://127.0.0.1:5173> and expects the API at `http://127.0.0.1:8000` by default. Override the API URL with `VITE_API_URL`.

When the API is unavailable, the interface uses seeded preview threads so the exchange can be explored without setup.

## Production Build

```bash
npm run build
npm run preview
```
