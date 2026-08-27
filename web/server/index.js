/**
 * Express BFF for the Reclaim operations console.
 *
 * Deliberately thin. It serves the built client and forwards /api to the
 * Python service that owns the domain. It holds no business logic, opens no
 * database connection, and never interprets a domain response — money, case
 * state, verification, and audit remain the Python layer's authority.
 *
 * Provider credentials live only in the Python service's environment. Nothing
 * secret passes through here and nothing secret reaches the browser.
 */
import express from "express";
import { createProxyMiddleware } from "http-proxy-middleware";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const PORT = Number(process.env.PORT ?? 4000);
const API_TARGET = process.env.RECLAIM_API_URL ?? "http://127.0.0.1:8000";
const DIST = path.resolve(__dirname, "..", "dist");

const app = express();
app.disable("x-powered-by");

app.get("/healthz", (_req, res) => {
  res.json({ status: "ok", proxying: API_TARGET });
});

// Mounted at the root with a path filter rather than `app.use("/api", ...)`:
// mounting on a path would strip the prefix before forwarding, and the API
// serves its routes under /api.
app.use(
  createProxyMiddleware({
    pathFilter: "/api",
    target: API_TARGET,
    changeOrigin: true,
    // A domain service that is down must not look like an empty result set.
    on: {
      error: (err, _req, res) => {
        if (res && "writeHead" in res && !res.headersSent) {
          res.writeHead(502, { "Content-Type": "application/json" });
        }
        if (res && "end" in res) {
          res.end(
            JSON.stringify({
              detail:
                "The recovery service is unreachable. Data shown may be stale.",
              cause: err.message,
            }),
          );
        }
      },
    },
  }),
);

app.use(express.static(DIST, { index: false, maxAge: "1h" }));

// Client-side routing: unknown non-API paths resolve to the app shell.
app.get("*", (req, res) => {
  if (req.path.startsWith("/api")) {
    res.status(404).json({ detail: "Unknown API route." });
    return;
  }
  res.sendFile(path.join(DIST, "index.html"), (err) => {
    if (err) {
      res
        .status(503)
        .type("text/plain")
        .send("Client bundle not built. Run `npm run build`.");
    }
  });
});

app.listen(PORT, () => {
  console.log(`reclaim console  http://localhost:${PORT}  ->  ${API_TARGET}`);
});
