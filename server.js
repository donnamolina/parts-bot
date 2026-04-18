/**
 * Parts-Bot WhatsApp Server (v11.3 — agentic loop is the only path)
 *
 * Thin Baileys adapter. Receives WhatsApp messages, normalises them,
 * and forwards to the Python agent subprocess. Sends back whatever the
 * agent returns (text + optional Excel files).
 *
 * Stack: Baileys (WhatsApp Web API) + Python agent (agent/run_agent.py)
 * Port: 3002 (isolated from cancha-bot on 3000)
 */

require("dotenv").config();
const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
} = require("@whiskeysockets/baileys");
const pino = require("pino");
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");
const { createClient } = require("@supabase/supabase-js");

// ─── Supabase client ─────────────────────────────────────────────────────────

const supabase = createClient(
  process.env.SUPABASE_URL,
  process.env.SUPABASE_ANON_KEY,
);

// ─── Config ──────────────────────────────────────────────────────────────────

const ALLOWED_NUMBERS = (process.env.ALLOWED_NUMBERS || "")
  .split(",")
  .map(n => n.trim().replace(/[^0-9]/g, ""))
  .filter(Boolean);

const AUTH_DIR   = path.join(__dirname, "auth_info");
const OUTPUT_DIR = path.join(__dirname, "output");
const LOG_DIR    = path.join(__dirname, "logs");
const PYTHON     = path.join(__dirname, ".venv", "bin", "python3");

const V11_INTERNAL_PORT    = parseInt(process.env.V11_INTERNAL_PORT        || "3019", 10);
const V11_AGENT_SCRIPT     = path.join(__dirname, "agent", "run_agent.py");
const V11_TURN_TIMEOUT_MS  = parseInt(process.env.V11_TURN_TIMEOUT_SECONDS || "300",  10) * 1000;

// ─── formatPartsList helper ─────────────────────────────────────────────────
// Kept for agent tools that may call this for debug / logging output.
function formatPartsList(parts, vehicle, opts = {}) {
  const {
    header    = null,
    title     = "📋 *Piezas:*",
    showEn    = true,
    showQty   = true,
    showPrice = true,
    footer    = "",
  } = opts;

  let out = "";
  if (header) out += header;
  out += `${title}\n`;
  out += "─────────────────────\n";

  parts.forEach((p, i) => {
    const side  = p.side     ? ` (${p.side === "left" ? "Izq" : "Der"})`           : "";
    const pos   = p.position ? ` ${p.position === "front" ? "Del" : "Tras"}`       : "";
    const en    = (showEn    && p.name_english)                  ? ` → ${p.name_english}`                  : "";
    const qty   = (showQty   && p.quantity && p.quantity > 1)   ? ` x${p.quantity}`                        : "";
    const price = (showPrice && p.local_price)                   ? ` — RD$${p.local_price.toLocaleString()}` : "";
    out += `${i + 1}. ${p.name_original || p.name_dr}${side}${pos}${en}${qty}${price}\n`;
  });

  out += "─────────────────────\n";
  if (footer) out += footer;
  return out;
}

// Ensure directories exist
[AUTH_DIR, OUTPUT_DIR, LOG_DIR].forEach(d => fs.mkdirSync(d, { recursive: true }));

// Logger
const logger     = pino({ level: "info" }, pino.destination(path.join(LOG_DIR, "bot.log")));
const consoleLog = (...args) => { console.log(new Date().toISOString(), ...args); };

// ─── WhatsApp Helpers ───────────────────────────────────────────────────────

function isAllowed(jid) {
  if (ALLOWED_NUMBERS.length === 0) return true;

  // LID format (@lid) — Baileys v6; private number so allow all and log.
  if (jid.includes("@lid")) {
    consoleLog(`Auth: LID user ${jid} — allowed (private number)`);
    return true;
  }

  const num    = jid.replace(/[^0-9]/g, "").replace(/@.*/, "");
  const last10 = num.slice(-10);
  const allowed = ALLOWED_NUMBERS.some(a => {
    const aLast10 = a.slice(-10);
    return last10 === aLast10 || num.includes(a) || a.includes(num);
  });
  consoleLog(`Auth check: jid=${jid} num=${num} allowed=${allowed}`);
  return allowed;
}

async function sendText(sock, jid, text) {
  try {
    await sock.sendMessage(jid, { text });
  } catch (e) {
    consoleLog("Send error:", e.message);
  }
}

async function sendDocument(sock, jid, filePath, fileName) {
  try {
    const buffer = fs.readFileSync(filePath);
    await sock.sendMessage(jid, {
      document: buffer,
      fileName,
      mimetype: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
  } catch (e) {
    consoleLog("Send document error:", e.message);
  }
}

// ─── v11 Agent Turn ─────────────────────────────────────────────────────────
// Spawns agent/run_agent.py, pipes a JSON payload on stdin, reads the JSON
// result on stdout, then dispatches text / files back to the user.
async function handleAgentTurn(sock, jid, userText, attachments) {
  const phone   = (jid || "").replace(/[^0-9]/g, "").replace(/@.*/, "");
  const payload = {
    user_id:     phone,
    message:     userText || "",
    attachments: (attachments || []).map(a => ({
      path: a.path,
      type: a.type || a.mime || "",
      mime: a.mime || a.type || "",
    })),
  };

  const env = {
    ...process.env,
    PYTHONPATH:          __dirname,
    V11_INTERNAL_PORT:   String(V11_INTERNAL_PORT),
    AGENT_JID:           jid || "",
  };

  const started = Date.now();
  consoleLog(`[v11] agent turn begin phone=${phone} text_len=${(userText || "").length} attachments=${payload.attachments.length}`);

  const result = await new Promise((resolve) => {
    let settled = false;

    const proc = spawn(PYTHON, [V11_AGENT_SCRIPT], { cwd: __dirname, env });
    let stdout = "";
    let stderr = "";

    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try { proc.kill("SIGKILL"); } catch (_) { }
      consoleLog(`[v11] agent turn TIMEOUT after ${V11_TURN_TIMEOUT_MS}ms phone=${phone}`);
      resolve({ text: "Disculpa, la búsqueda está tardando más de lo normal. Intenta de nuevo en un momento.", files: [], _timeout: true });
    }, V11_TURN_TIMEOUT_MS);

    proc.stdout.on("data", d => { stdout += d.toString(); });
    proc.stderr.on("data", d => { stderr += d.toString(); });

    proc.on("close", code => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (code !== 0) {
        consoleLog(`[v11] agent exit code=${code} phone=${phone} stderr=${stderr.slice(-500)}`);
        resolve({ text: "Disculpa, hubo un error procesando tu mensaje. Intenta de nuevo.", files: [], _exit_code: code });
        return;
      }
      try {
        resolve(JSON.parse(stdout.trim().split("\n").pop() || "{}"));
      } catch (e) {
        consoleLog(`[v11] agent stdout parse error: ${e.message} raw=${stdout.slice(0, 400)}`);
        resolve({ text: "Disculpa, hubo un error procesando tu mensaje. Intenta de nuevo.", files: [], _parse_error: e.message });
      }
    });

    proc.on("error", e => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      consoleLog(`[v11] agent spawn error: ${e.message}`);
      resolve({ text: "Disculpa, hubo un error procesando tu mensaje. Intenta de nuevo.", files: [], _spawn_error: e.message });
    });

    try {
      proc.stdin.write(JSON.stringify(payload));
      proc.stdin.end();
    } catch (e) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      consoleLog(`[v11] agent stdin error: ${e.message}`);
      resolve({ text: "Disculpa, hubo un error procesando tu mensaje. Intenta de nuevo.", files: [], _stdin_error: e.message });
    }
  });

  const elapsedMs = Date.now() - started;
  consoleLog(`[v11] agent turn done phone=${phone} elapsed_ms=${elapsedMs} text_len=${(result.text || "").length} files=${(result.files || []).length}`);

  if (result.text && String(result.text).trim()) {
    await sendText(sock, jid, String(result.text));
  }
  for (const f of result.files || []) {
    if (!f || !f.path) continue;
    try {
      await sendDocument(sock, jid, f.path, f.name || path.basename(f.path));
    } catch (e) {
      consoleLog(`[v11] sendDocument error: ${e.message}`);
    }
  }
}

// ─── v11 Internal HTTP Endpoint ─────────────────────────────────────────────
// Loopback-only surface that Python tools (send_document) use to push files
// out of band without needing a return value on stdin/stdout.
function startV11InternalServer(getSock) {
  const http   = require("http");
  const server = http.createServer(async (req, res) => {
    if (req.method !== "POST" || req.url !== "/internal/send-document") {
      res.writeHead(404, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ ok: false, error: "not_found" }));
      return;
    }
    let body = "";
    req.on("data", chunk => { body += chunk.toString(); if (body.length > 1_000_000) req.destroy(); });
    req.on("end", async () => {
      try {
        const j        = JSON.parse(body || "{}");
        const jid      = j.jid || (j.phone ? `${String(j.phone).replace(/[^0-9]/g, "")}@s.whatsapp.net` : null);
        const filePath = j.path;
        const fileName = j.name || (filePath ? path.basename(filePath) : "document");
        if (!jid || !filePath || !fs.existsSync(filePath)) {
          res.writeHead(400, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ ok: false, error: "bad_payload" }));
          return;
        }
        const sock = typeof getSock === "function" ? getSock() : null;
        if (!sock) {
          res.writeHead(503, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ ok: false, error: "sock_unavailable" }));
          return;
        }
        await sendDocument(sock, jid, filePath, fileName);
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ ok: true }));
      } catch (e) {
        res.writeHead(500, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ ok: false, error: e.message }));
      }
    });
  });
  server.listen(V11_INTERNAL_PORT, "127.0.0.1", () => {
    consoleLog(`[v11] internal HTTP endpoint listening on 127.0.0.1:${V11_INTERNAL_PORT}`);
  });
  server.on("error", e => { consoleLog(`[v11] internal HTTP error: ${e.message}`); });
  return server;
}

// ─── Message Handler ────────────────────────────────────────────────────────

async function handleMessage(sock, msg) {
  const jid = msg.key.remoteJid;
  if (!jid || jid === "status@broadcast") return;
  if (msg.key.fromMe) return;

  if (!isAllowed(jid)) {
    await sendText(sock, jid, "⛔ Tu número no está autorizado para usar este bot.");
    return;
  }

  const message = msg.message;
  if (!message) return;

  const userText = message.conversation
    || message.extendedTextMessage?.text
    || message.imageMessage?.caption
    || message.documentMessage?.caption
    || "";

  const hasImage = !!message.imageMessage;
  const hasPdf   = !!(message.documentMessage && (message.documentMessage.mimetype || "").includes("pdf"));

  if (!userText.trim() && !hasImage && !hasPdf) {
    consoleLog(`[v11] ignoring empty message from ${jid}`);
    return;
  }

  const attachments = [];
  if (hasImage || hasPdf) {
    const ext  = hasImage ? ".jpg" : ".pdf";
    const mime = hasImage
      ? (message.imageMessage.mimetype || "image/jpeg")
      : (message.documentMessage.mimetype || "application/pdf");
    try {
      const buffer    = await downloadMediaMessage(msg, "buffer", {});
      const mediaPath = path.join(OUTPUT_DIR, `v11_${Date.now()}${ext}`);
      fs.writeFileSync(mediaPath, buffer);
      attachments.push({ path: mediaPath, type: mime, mime });
    } catch (e) {
      consoleLog(`media download error: ${e.message}`);
      await sendText(sock, jid, "Uy, no pude descargar tu archivo. Intenta de nuevo.");
      return;
    }
  }

  try {
    await handleAgentTurn(sock, jid, userText, attachments);
  } catch (e) {
    consoleLog(`handleAgentTurn error: ${e.message}`);
    await sendText(sock, jid, "Disculpa, hubo un error procesando tu mensaje. Intenta de nuevo.");
  }
}

// ─── Weekly Report ──────────────────────────────────────────────────────────

let _globalSock = null;

async function sendWeeklyReport() {
  if (!_globalSock) return;
  const matthewJid = `${(process.env.ALLOWED_NUMBERS || "").split(",")[0].replace(/[^0-9]/g, "")}@s.whatsapp.net`;
  if (!matthewJid || matthewJid === "@s.whatsapp.net") return;

  try {
    const oneWeekAgo = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString();

    const [
      { count: searchCount },
      { count: corrCount },
      { data: corrections },
      { data: vehicles },
      { count: cacheCount },
    ] = await Promise.all([
      supabase.from("parts_sessions").select("*",                                    { count: "exact", head: true }).gte("created_at", oneWeekAgo),
      supabase.from("parts_corrections").select("*",                                 { count: "exact", head: true }).gte("created_at", oneWeekAgo),
      supabase.from("parts_corrections").select("part_name_original,part_name_corrected,auto_promoted").eq("auto_promoted", true).gte("created_at", oneWeekAgo),
      supabase.from("parts_sessions").select("vehicle_make,vehicle_model").gte("created_at", oneWeekAgo),
      supabase.from("parts_cache").select("*",                                       { count: "exact", head: true }),
    ]);

    const vehicleCounts = {};
    (vehicles || []).forEach(v => {
      const key = `${v.vehicle_make || "?"} ${v.vehicle_model || "?"}`;
      vehicleCounts[key] = (vehicleCounts[key] || 0) + 1;
    });
    const topVehicle = Object.entries(vehicleCounts).sort((a, b) => b[1] - a[1])[0];

    const { count: sessionsWithCorrections } = await supabase
      .from("parts_corrections")
      .select("*", { count: "exact", head: true })
      .gte("created_at", oneWeekAgo);
    const sessTotal    = searchCount || 0;
    const accuracyPct  = sessTotal > 0
      ? Math.round(((sessTotal - Math.min(sessionsWithCorrections || 0, sessTotal)) / sessTotal) * 100)
      : 0;

    const newTerms = (corrections || []).map(c => `  • ${c.part_name_original} → ${c.part_name_corrected}`).join("\n") || "  Ninguno";

    const report =
      `📊 *Reporte semanal del buscador de piezas*\n` +
      `🔍 Búsquedas: ${searchCount || 0}\n` +
      `✏️ Correcciones: ${corrCount || 0}\n` +
      `📚 Términos nuevos al diccionario: ${(corrections || []).length}\n${newTerms}\n` +
      `🚗 Vehículo más buscado: ${topVehicle ? `${topVehicle[0]} (${topVehicle[1]}x)` : "N/A"}\n` +
      `📈 Precisión sin corrección: ${accuracyPct}%\n` +
      `✅ Piezas verificadas en caché: ${cacheCount || 0}`;

    await sendText(_globalSock, matthewJid, report);
    consoleLog("Weekly report sent");
  } catch (e) {
    consoleLog("Weekly report error:", e.message);
  }
}

function scheduleWeeklyReport() {
  const now           = new Date();
  const nextMonday    = new Date(now);
  const day           = now.getUTCDay();
  const daysUntilMon  = day === 1 ? 7 : (8 - day) % 7;
  nextMonday.setUTCDate(now.getUTCDate() + daysUntilMon);
  nextMonday.setUTCHours(12, 0, 0, 0);
  const msUntilFirst = nextMonday.getTime() - now.getTime();
  consoleLog(`Weekly report scheduled in ${Math.round(msUntilFirst / 3600000)}h`);
  setTimeout(() => {
    sendWeeklyReport();
    setInterval(sendWeeklyReport, 7 * 24 * 60 * 60 * 1000);
  }, msUntilFirst);
}

// ─── Bot Connection ─────────────────────────────────────────────────────────

async function startBot() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version }          = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth:             state,
    logger:           pino({ level: "silent" }),
    browser:          ["Parts-Bot", "Chrome", "120.0.0"],
    syncFullHistory:  false,
  });

  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      const QRCode = require("qrcode");
      const qrPath = path.join(__dirname, "qr.png");
      QRCode.toFile(qrPath, qr, { width: 600, margin: 2 }, (err) => {
        if (err) consoleLog("QR save error:", err.message);
        else     consoleLog(`✅ QR code saved to: ${qrPath} — open it and scan with WhatsApp`);
      });
    }

    if (connection === "close") {
      const reason          = lastDisconnect?.error?.output?.statusCode;
      const shouldReconnect = reason !== DisconnectReason.loggedOut;
      consoleLog(`Connection closed. Reason: ${reason}. Reconnecting: ${shouldReconnect}`);
      if (shouldReconnect) {
        setTimeout(startBot, 5000);
      } else {
        consoleLog("Logged out. Delete auth_info/ and restart to re-authenticate.");
      }
    } else if (connection === "open") {
      consoleLog("✅ Parts-Bot connected to WhatsApp");
      consoleLog(`📋 Allowed numbers: ${ALLOWED_NUMBERS.length > 0 ? ALLOWED_NUMBERS.join(", ") : "ALL (no whitelist)"}`);
      _globalSock = sock;
      if (!global._v11_internal_started) {
        try {
          startV11InternalServer(() => _globalSock);
          global._v11_internal_started = true;
          consoleLog("[v11] internal HTTP server started");
        } catch (e) {
          consoleLog(`[v11] internal server startup error: ${e.message}`);
        }
      }
    }
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    for (const msg of messages) {
      try {
        await handleMessage(sock, msg);
      } catch (e) {
        consoleLog("Message handler error:", e.message);
        logger.error({ err: e, msg: msg.key }, "Message handler error");
      }
    }
  });
}

// ─── Start ──────────────────────────────────────────────────────────────────

consoleLog("🚀 Starting Parts-Bot on port", process.env.PORT || 3002);
scheduleWeeklyReport();
startBot().catch(e => {
  consoleLog("Fatal error:", e);
  process.exit(1);
});
